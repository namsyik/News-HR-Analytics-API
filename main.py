from fastapi import FastAPI, HTTPException, Header, Query, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
import hmac
import secrets
import logging

import jwt as pyjwt
from pydantic_settings import BaseSettings, SettingsConfigDict

from db import (
    count_articles,
    count_articles_by_source,
    get_articles,
    get_article_by_id,
    get_attrition_summary,
    get_attrition_by_department,
    get_attrition_by_overtime,
    get_attrition_by_tenure,
    get_top_earners_by_department,
    search_articles,             # TASK 1
    get_department_detail,       # TASK 3
    get_high_risk_attrition,     # TASK 4
)

log = logging.getLogger(__name__)

class Settings(BaseSettings):
    database_url: str
    student_name: str
    internal_api_key: str
    jwt_secret: str
    jwt_expiry_minutes: int = 30
    client_id: str
    client_secret: str

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )

settings = Settings()

app = FastAPI(
    title="News & HR Analytics API",
    description="Session 11 — Mini Project 2 Solution",
    version="1.0",
)
security_scheme = HTTPBearer(auto_error=False)

class Article(BaseModel):
    id: int
    title: str
    url: str
    source: str
    content: Optional[str] = None
    published_at: Optional[datetime] = None
    scraped_at: Optional[datetime] = None

# TASK 1 Pydantic Model
class ArticleSearchResponse(BaseModel):
    data: List[Article]
    page: int
    per_page: int
    total_items: int
    total_pages: int

# TASK 2 Pydantic Model
class ArticleStats(BaseModel):
    total_articles: int
    by_source: Dict[str, int]

class TokenRequest(BaseModel):
    client_id: str
    client_secret: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int

class AttritionSummary(BaseModel):
    total_employees: int
    attrition_yes: int
    attrition_no: int
    attrition_rate: float

class DepartmentAttrition(BaseModel):
    Department: str
    total: int
    attrition_count: int
    attrition_rate: float
    avg_income: float

# TASK 3 Pydantic Models
class TopEarnerDept(BaseModel):
    EmployeeNumber: int
    JobRole: str
    MonthlyIncome: int

class DepartmentDetailResponse(BaseModel):
    department: str
    total_employees: int
    attrition_count: int
    attrition_rate: float
    avg_income: float
    min_income: int
    max_income: int
    top_earners: List[TopEarnerDept]

class OvertimeAttrition(BaseModel):
    OverTime: str
    total: int
    attrition_count: int
    attrition_rate: float

class TenureAttrition(BaseModel):
    tenure_bucket: str
    total: int
    attrition_count: int
    attrition_rate: float

class TopEarner(BaseModel):
    EmployeeNumber: int
    Department: str
    JobRole: str
    MonthlyIncome: int
    Attrition: str
    income_rank: int


# ==================================================================
# Authentication Helpers
# ==================================================================

def create_access_token(client_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": client_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expiry_minutes),
        "jti": secrets.token_hex(16),
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")

def verify_jwt_token(token: str) -> dict:
    try:
        return pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token sudah expired")
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token tidak valid")

def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if x_api_key is None:
        return None
    if hmac.compare_digest(x_api_key, settings.internal_api_key):
        return {"auth_method": "api_key"}
    raise HTTPException(status_code=401, detail="API Key tidak valid")

def get_current_client(
    api_key_result=Depends(verify_api_key),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
):
    if api_key_result is not None:
        return api_key_result
    if credentials is not None:
        payload = verify_jwt_token(credentials.credentials)
        return {"auth_method": "bearer", "client_id": payload["sub"]}
    raise HTTPException(
        status_code=401,
        detail="Authentication Required. Gunakan API Key atau JWT",
        headers={"WWW-Authenticate": "Bearer"},
    )

# ==================================================================
# PUBLIC ENDPOINTS
# ==================================================================
@app.get("/", tags=["Public"])
def read_root():
    return {
        "message": "News & HR Analytics API — Session 11",
        "student": settings.student_name,
        "paths": {
            "etl_articles": "/articles",
            "elt_attrition": "/attrition",
            "docs": "/docs",
        },
    }

@app.get("/health", tags=["Public"])
def health_check():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}

# ==================================================================
# AUTH ENDPOINT
# ==================================================================
@app.post("/token", response_model=TokenResponse, tags=["Auth"])
def login_for_token(req: TokenRequest):
    valid_id = hmac.compare_digest(req.client_id, settings.client_id)
    valid_secret = hmac.compare_digest(req.client_secret, settings.client_secret)
    if not (valid_id and valid_secret):
        raise HTTPException(status_code=401, detail="Invalid client credentials")

    token = create_access_token(req.client_id)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in=settings.jwt_expiry_minutes * 60,
    )

# ==================================================================
# ETL PATH: Article Endpoints
# ==================================================================

# TASK 1: Endpoint GET /articles/search
@app.get("/articles/search", response_model=ArticleSearchResponse, tags=["Articles (ETL)"])
def article_search(
    source: Optional[str] = Query(None, description="Filter by source (misal: bbc, nytimes)"),
    title: Optional[str] = Query(None, description="Case-insensitive partial match pada judul"),
    date_from: Optional[str] = Query(None, description="Format: YYYY-MM-DD, filter published_at >="),
    date_to: Optional[str] = Query(None, description="Format: YYYY-MM-DD, filter published_at <="),
    page: int = Query(1, ge=1, description="Nomor halaman (mulai dari 1)"),
    per_page: int = Query(10, ge=1, le=50, description="Jumlah artikel per halaman (max 50)"),
    auth=Depends(get_current_client),
):
    """Article Search dengan Pagination."""
    return search_articles(
        source=source,
        title=title,
        date_from=date_from,
        date_to=date_to,
        page=page,
        per_page=per_page,
    )

# TASK 2: Endpoint GET /articles/stats
@app.get("/articles/stats", response_model=ArticleStats, tags=["Articles (ETL)"])
def article_stats(auth=Depends(get_current_client)):
    """Total artikel dan breakdown per source."""
    return ArticleStats(
        total_articles=count_articles(),
        by_source=count_articles_by_source(),
    )

@app.get("/articles", response_model=List[Article], tags=["Articles (ETL)"])
def list_articles(
    source: Optional[str] = Query(None, description="Filter by source"),
    title: Optional[str] = Query(None, description="Search title"),
    limit: int = Query(20, le=100, ge=1, description="Max results"),
    auth=Depends(get_current_client),
):
    return get_articles(source=source, title=title, limit=limit)

@app.get("/articles/{article_id}", response_model=Article, tags=["Articles (ETL)"])
def get_single_article(
    article_id: int,
    auth=Depends(get_current_client),
):
    article = get_article_by_id(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article

# ==================================================================
# ELT PATH: Attrition Endpoints
# ==================================================================
@app.get("/attrition/summary", response_model=AttritionSummary, tags=["Attrition (ELT)"])
def attrition_summary(auth=Depends(get_current_client)):
    return get_attrition_summary()

@app.get(
    "/attrition/by-department",
    response_model=List[DepartmentAttrition],
    tags=["Attrition (ELT)"],
)
def attrition_by_department_summary(auth=Depends(get_current_client)):
    return get_attrition_by_department()

# TASK 3: Endpoint GET /attrition/department/{dept_name}
@app.get(
    "/attrition/department/{dept_name}",
    response_model=DepartmentDetailResponse,
    tags=["Attrition (ELT)"],
)
def attrition_department_detail(
    dept_name: str,
    auth=Depends(get_current_client),
):
    """Statistik lengkap per department."""
    dept_detail = get_department_detail(dept_name)
    if not dept_detail:
        raise HTTPException(status_code=404, detail=f"Department '{dept_name}' not found")
    return dept_detail

# TASK 4: Endpoint GET /attrition/risk-profile
@app.get(
    "/attrition/risk-profile",
    response_model=List[Dict[str, Any]],
    tags=["Attrition (ELT)"],
)
def attrition_risk_profile(
    limit: int = Query(20, ge=1, le=100, description="Max high-risk records (1-100)"),
    auth=Depends(get_current_client),
):
    """Karyawan dengan indikator risiko attrition tinggi."""
    return get_high_risk_attrition(limit=limit)

@app.get(
    "/attrition/by-overtime",
    response_model=List[OvertimeAttrition],
    tags=["Attrition (ELT)"],
)
def attrition_by_overtime(auth=Depends(get_current_client)):
    return get_attrition_by_overtime()

@app.get(
    "/attrition/by-tenure",
    response_model=List[TenureAttrition],
    tags=["Attrition (ELT)"],
)
def attrition_by_tenure(auth=Depends(get_current_client)):
    return get_attrition_by_tenure()

@app.get(
    "/attrition/top-earners",
    response_model=List[TopEarner],
    tags=["Attrition (ELT)"],
)
def top_earners(
    limit_per_dept: int = Query(5, le=20, ge=1, description="Top N per department"),
    auth=Depends(get_current_client),
):
    return get_top_earners_by_department(limit_per_dept=limit_per_dept)