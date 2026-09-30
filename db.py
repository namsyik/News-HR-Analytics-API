import math
from typing import Optional, List, Dict
from sqlalchemy import create_engine, text
from pydantic_settings import BaseSettings, SettingsConfigDict
import pandas as pd
import logging

log = logging.getLogger(__name__)

class DBSettings(BaseSettings):
    database_url: str
    student_name: str

    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=False, extra="ignore"
    )

settings = DBSettings()
engine = create_engine(settings.database_url)

SCHEMA = "ai_engineer"
ARTICLES_TABLE = f'"{SCHEMA}".articles_{settings.student_name}'
ATTRITION_TABLE_NAME = f"raw_attrition_{settings.student_name}"
ATTRITION_TABLE = f'"{SCHEMA}".{ATTRITION_TABLE_NAME}'

def init_db():
    """Create tables and indexes if they don't exist."""
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {ARTICLES_TABLE} (
        id           SERIAL PRIMARY KEY,
        title        TEXT NOT NULL,
        url          TEXT UNIQUE NOT NULL,
        source       TEXT NOT NULL,
        content      TEXT,
        published_at TIMESTAMP,
        scraped_at   TIMESTAMP DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_articles_source
        ON {ARTICLES_TABLE}(source);
    CREATE INDEX IF NOT EXISTS idx_articles_published
        ON {ARTICLES_TABLE}(published_at DESC);
    """
    with engine.begin() as conn:
        conn.execute(text(ddl))
    log.info("Database ready. Table: %s", ARTICLES_TABLE)

def save_articles(df: pd.DataFrame) -> int:
    """Insert articles with ON CONFLICT — safe to re-run."""
    if df.empty:
        log.warning("DataFrame kosong, tidak ada yang di-insert.")
        return 0

    inserted = 0
    with engine.begin() as conn:
        for _, row in df.iterrows():
            result = conn.execute(
                text(f"""
                    INSERT INTO {ARTICLES_TABLE}
                        (title, url, source, content, published_at)
                    VALUES
                        (:title, :url, :source, :content, :published_at)
                    ON CONFLICT (url) DO NOTHING
                """),
                {
                    "title": row["title"],
                    "url": row["url"],
                    "source": row["source"],
                    "content": row["content"],
                    "published_at": row["published_at"]
                    if pd.notna(row["published_at"])
                    else None,
                },
            )
            inserted += result.rowcount

    log.info("Insert selesai: %d baru dari %d record.", inserted, len(df))
    return inserted

def count_articles() -> int:
    """Count total articles in DB."""
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT COUNT(*) FROM {ARTICLES_TABLE}"))
        return result.scalar()

def count_articles_by_source() -> Dict[str, int]:
    """Hitung jumlah artikel per source (TASK 2)."""
    with engine.connect() as conn:
        rows = conn.execute(text(
            f"SELECT source, COUNT(*) as cnt FROM {ARTICLES_TABLE}"
            f" GROUP BY source ORDER BY cnt DESC"
        )).mappings().all()
    return {row["source"]: row["cnt"] for row in rows}

# TASK 1: Article Search dengan Pagination
def search_articles(
    source: Optional[str] = None,
    title: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    per_page: int = 10,
) -> Dict:
    where_clauses = ["1=1"]
    params = {}

    if source:
        where_clauses.append("source = :source")
        params["source"] = source.lower()

    if title:
        where_clauses.append("title ILIKE :title")
        params["title"] = f"%{title}%"

    if date_from:
        where_clauses.append("published_at >= :date_from::timestamp")
        params["date_from"] = date_from

    if date_to:
        where_clauses.append("published_at <= :date_to::timestamp + interval '1 day'")
        params["date_to"] = date_to

    where_sql = " AND ".join(where_clauses)

    count_query = f"SELECT COUNT(*) FROM {ARTICLES_TABLE} WHERE {where_sql}"
    
    offset = (page - 1) * per_page
    data_query = f"""
        SELECT * FROM {ARTICLES_TABLE}
        WHERE {where_sql}
        ORDER BY published_at DESC NULLS LAST
        LIMIT :limit OFFSET :offset
    """
    params["limit"] = per_page
    params["offset"] = offset

    with engine.connect() as conn:
        total_items = conn.execute(text(count_query), params).scalar()
        rows = conn.execute(text(data_query), params).mappings().all()

    total_pages = math.ceil(total_items / per_page) if total_items > 0 else 0

    return {
        "data": [dict(r) for r in rows],
        "page": page,
        "per_page": per_page,
        "total_items": total_items,
        "total_pages": total_pages,
    }

def get_articles(
    source: Optional[str] = None,
    title: Optional[str] = None,
    limit: int = 20,
) -> List[Dict]:
    query = f"SELECT * FROM {ARTICLES_TABLE} WHERE 1=1"
    params: dict = {}

    if source:
        query += " AND source = :source"
        params["source"] = source.lower()

    if title:
        query += " AND title ILIKE :title"
        params["title"] = f"%{title}%"

    query += " ORDER BY published_at DESC NULLS LAST LIMIT :limit"
    params["limit"] = limit

    with engine.connect() as conn:
        rows = conn.execute(text(query), params).mappings().all()
    return [dict(r) for r in rows]

def get_article_by_id(article_id: int) -> Optional[Dict]:
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT * FROM {ARTICLES_TABLE} WHERE id = :id"),
            {"id": article_id},
        ).mappings().first()
    return dict(row) if row else None

# TASK 3: Department Detail Endpoint
def get_department_detail(dept_name: str) -> Optional[Dict]:
    with engine.connect() as conn:
        stats_query = f"""
            SELECT
                "Department",
                COUNT(*) AS total_employees,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_count,
                ROUND(COUNT(*) FILTER (WHERE "Attrition" = 'Yes')::numeric / COUNT(*), 4) AS attrition_rate,
                ROUND(AVG("MonthlyIncome")::numeric, 2) AS avg_income,
                MIN("MonthlyIncome") AS min_income,
                MAX("MonthlyIncome") AS max_income
            FROM {ATTRITION_TABLE}
            WHERE LOWER("Department") = LOWER(:dept_name)
            GROUP BY "Department"
        """
        stats_row = conn.execute(text(stats_query), {"dept_name": dept_name}).mappings().first()
        if not stats_row:
            return None

        top_earners_query = f"""
            SELECT "EmployeeNumber", "JobRole", "MonthlyIncome"
            FROM {ATTRITION_TABLE}
            WHERE LOWER("Department") = LOWER(:dept_name)
            ORDER BY "MonthlyIncome" DESC
            LIMIT 3
        """
        top_earners = conn.execute(text(top_earners_query), {"dept_name": dept_name}).mappings().all()

    return {
        "department": stats_row["Department"],
        "total_employees": stats_row["total_employees"],
        "attrition_count": stats_row["attrition_count"],
        "attrition_rate": float(stats_row["attrition_rate"]),
        "avg_income": float(stats_row["avg_income"]),
        "min_income": stats_row["min_income"],
        "max_income": stats_row["max_income"],
        "top_earners": [dict(r) for r in top_earners],
    }

# TASK 4: Attrition Risk Profile
def get_high_risk_attrition(limit: int = 20) -> List[Dict]:
    query = f"""
        WITH dept_avg AS (
            SELECT "Department", AVG("MonthlyIncome") AS avg_income
            FROM {ATTRITION_TABLE}
            GROUP BY "Department"
        )
        SELECT e.*
        FROM {ATTRITION_TABLE} e
        JOIN dept_avg d ON e."Department" = d."Department"
        WHERE e."OverTime" = 'Yes'
          AND e."YearsAtCompany" < 3
          AND e."MonthlyIncome" < d.avg_income
        LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(query), {"limit": limit}).mappings().all()
    return [dict(r) for r in rows]

def get_attrition_summary() -> Dict:
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT 
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_yes
            FROM {ATTRITION_TABLE}
        """)).mappings().one()
        total = row["total"]
        attrition_yes = row["attrition_yes"]
    return {
        "total_employees": total,
        "attrition_yes": attrition_yes,
        "attrition_no": total - attrition_yes,
        "attrition_rate": round(attrition_yes / total, 4) if total > 0 else 0.0,
    }

def get_attrition_by_department() -> List[Dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                "Department",
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_count,
                ROUND(
                    COUNT(*) FILTER (WHERE "Attrition" = 'Yes')::numeric / COUNT(*), 3
                ) AS attrition_rate,
                ROUND(AVG("MonthlyIncome")::numeric, 2) AS avg_income
            FROM {ATTRITION_TABLE}
            GROUP BY "Department"
            ORDER BY attrition_rate DESC
        """)).mappings().all()
    return [dict(r) for r in rows]

def get_attrition_by_tenure() -> List[Dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                CASE
                    WHEN "YearsAtCompany" < 2 THEN '1. new (<2y)'
                    WHEN "YearsAtCompany" < 5 THEN '2. mid (2-5y)'
                    ELSE '3. veteran (5y+)'
                END AS tenure_bucket,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_count,
                ROUND(
                    COUNT(*) FILTER (WHERE "Attrition" = 'Yes')::numeric / COUNT(*), 3
                ) AS attrition_rate
            FROM {ATTRITION_TABLE}
            GROUP BY tenure_bucket
            ORDER BY tenure_bucket
        """)).mappings().all()
    return [dict(r) for r in rows]

def get_attrition_by_overtime() -> List[Dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT
                "OverTime",
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE "Attrition" = 'Yes') AS attrition_count,
                ROUND(
                    COUNT(*) FILTER (WHERE "Attrition" = 'Yes')::numeric / COUNT(*), 3
                ) AS attrition_rate
            FROM {ATTRITION_TABLE}
            GROUP BY "OverTime"
            ORDER BY "OverTime"
        """)).mappings().all()
    return [dict(r) for r in rows]

def get_top_earners_by_department(limit_per_dept: int = 5) -> List[Dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT *
                FROM (
                    SELECT
                        "EmployeeNumber",
                        "Department",
                        "JobRole",
                        "MonthlyIncome",
                        "Attrition",
                        RANK() OVER (
                            PARTITION BY "Department"
                            ORDER BY "MonthlyIncome" DESC
                        ) AS income_rank
                    FROM {ATTRITION_TABLE}
                ) ranked
                WHERE income_rank <= :limit_per_dept
                ORDER BY "Department", income_rank
            """),
            {"limit_per_dept": limit_per_dept},
        ).mappings().all()
    return [dict(r) for r in rows]