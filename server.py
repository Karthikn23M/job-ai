"""
Naukri + LinkedIn Job Search MCP Server

- Naukri: wraps muhammetakkurtt/naukri-job-scraper
- LinkedIn: wraps curious_coder/linkedin-jobs-scraper

Tools:
  - naukri_search_jobs      → fresh jobs (last 24 hours) on Naukri
  - naukri_get_job_details  → full job details + any emails found
  - linkedin_search_jobs    → fresh jobs (last 24 hours) on LinkedIn
                              + job poster / recruiter name when available
"""

import os
import re
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from typing import Optional, List

load_dotenv()

APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")

# ─── Actors ───────────────────────────────────────────────────────────────────
NAUKRI_ACTOR_ID = "alpcnRV9YI9lYVPWk"  # muhammetakkurtt/naukri-job-scraper
LINKEDIN_ACTOR_ID = "curious_coder/linkedin-jobs-scraper"

NAUKRI_RUN_URL = f"https://api.apify.com/v2/acts/{NAUKRI_ACTOR_ID}/run-sync-get-dataset-items"
LINKEDIN_RUN_URL = f"https://api.apify.com/v2/acts/{LINKEDIN_ACTOR_ID}/run-sync-get-dataset-items"

# ─── City Name → Naukri City ID (only requested cities) ───────────────────────
CITY_IDS = {
    "chennai": "183",
    "coimbatore": "184",
    "madurai": "9",
}


def resolve_city_id(name: str) -> Optional[str]:
    """Case-insensitive city-name → Naukri city ID lookup. Returns None if unknown."""
    if not name:
        return None
    return CITY_IDS.get(name.strip().lower())


mcp = FastMCP("naukri_linkedin_mcp")


# ─── Shared Helpers ───────────────────────────────────────────────────────────

async def call_apify(url: str, payload: dict) -> list:
    if not APIFY_API_TOKEN:
        raise ValueError("APIFY_API_TOKEN not set. Check your .env file.")

    async with httpx.AsyncClient(timeout=180.0) as client:
        response = await client.post(
            url,
            params={"token": APIFY_API_TOKEN},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            if "error" in data:
                raise RuntimeError(f"Apify error: {data.get('error')}")
            if "items" in data and isinstance(data["items"], list):
                return data["items"]
            return [data]
        return data if isinstance(data, list) else []


def extract_emails(text: str) -> List[str]:
    """Simple email extractor from any free text."""
    if not text:
        return []
    pattern = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
    return list(set(re.findall(pattern, str(text))))


def format_naukri_job(job: dict) -> str:
    lines = [
        f"**{job.get('title', 'N/A')}** at {job.get('companyName', 'N/A')}",
        f"  Location : {job.get('location', 'N/A')}",
        f"  Experience: {job.get('experience', 'N/A')}",
        f"  Salary   : {job.get('salary', 'Not disclosed')}",
        f"  Skills   : {job.get('tagsAndSkills', 'N/A')}",
        f"  Posted   : {job.get('footerPlaceholderLabel', 'N/A')}",
        f"  Job ID   : {job.get('jobId', 'N/A')}",
        f"  URL      : {job.get('jdURL', '')}",
    ]

    emails = []
    for field in ["recruiterEmail", "email", "contactEmail", "hrEmail"]:
        if job.get(field):
            emails.append(str(job[field]))

    desc = job.get("jobDescription") or ""
    if isinstance(job.get("jobDetails"), dict):
        desc = job["jobDetails"].get("description", desc) or desc
    emails.extend(extract_emails(desc))

    if emails:
        lines.append(f"  Emails   : {', '.join(sorted(set(emails)))}")

    return "\n".join(lines)


def handle_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        if e.response.status_code == 401:
            return "Error: Invalid Apify API token. Check your .env file."
        if e.response.status_code == 429:
            return "Error: Apify rate limit hit. Wait a moment and try again."
        return (
            f"Error: Apify API returned status {e.response.status_code}. "
            f"Body: {e.response.text[:500]}"
        )
    if isinstance(e, httpx.TimeoutException):
        return "Error: Request timed out. Apify may be slow — try again shortly."
    return f"Error: {type(e).__name__}: {str(e)}"


# ─── Tool 1: Naukri Search (last 24 hours only) ───────────────────────────────

class SearchInput(BaseModel):
    keyword: str = Field(
        ...,
        description="Job title or skill (e.g. 'python developer', 'data analyst')",
        min_length=1,
        max_length=100,
    )
    location: Optional[str] = Field(
        default=None,
        description=(
            "City: only 'Chennai', 'Coimbatore' or 'Madurai' are supported. "
            "Leave empty for all India."
        ),
    )
    max_jobs: int = Field(
        default=15,
        description="Number of jobs to return (1-50)",
        ge=1,
        le=50,
    )
    experience_years: Optional[str] = Field(
        default=None,
        description="Minimum years of experience (e.g. '0', '2', '5')",
    )


@mcp.tool(
    name="naukri_search_jobs",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def naukri_search_jobs(params: SearchInput) -> str:
    """
    Search Naukri.com for FRESH job listings posted in the LAST 24 HOURS.
    Supported cities: Chennai, Coimbatore, Madurai.
    Returns title, company, location, salary, skills, job ID, URL and any emails found.
    """
    payload = {
        "keyword": params.keyword,
        "maxJobs": max(params.max_jobs, 50),
        "fetchDetails": False,
        "freshness": "1",
        "sortBy": "date",
    }

    if params.experience_years:
        payload["experience"] = params.experience_years

    unknown_city_notice = ""
    if params.location:
        city_id = resolve_city_id(params.location)
        if city_id:
            payload["cities"] = [city_id]
        else:
            payload["keyword"] = f"{params.keyword} {params.location}"
            unknown_city_notice = (
                f"\n\n_Note: '{params.location}' is not supported "
                f"(only Chennai, Coimbatore, Madurai). "
                f"It was added to the keyword instead._\n"
            )

    try:
        jobs = await call_apify(NAUKRI_RUN_URL, payload)
    except Exception as e:
        return handle_error(e)

    if not jobs:
        return (
            f"No jobs found for '{params.keyword}' in the last 24 hours."
            + unknown_city_notice
        )

    jobs = jobs[: params.max_jobs]
    formatted = [format_naukri_job(job) for job in jobs]
    header = (
        f"Found {len(jobs)} **fresh (last 24h)** jobs for '{params.keyword}':"
        f"{unknown_city_notice}\n\n"
    )
    return header + "\n\n---\n\n".join(formatted)


# ─── Tool 2: Naukri Job Details ───────────────────────────────────────────────

class DetailsInput(BaseModel):
    job_id: str = Field(
        ...,
        description="The Naukri job ID (get it from naukri_search_jobs)",
        min_length=5,
        max_length=30,
    )


@mcp.tool(
    name="naukri_get_job_details",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def naukri_get_job_details(params: DetailsInput) -> str:
    """
    Fetch full details for a specific Naukri job by its job ID.
    Includes description, skills, company info and attempts to extract HR/recruiter emails.
    """
    payload = {
        "jobIds": [params.job_id],
        "fetchDetails": True,
        "maxJobs": 1,
    }

    try:
        jobs = await call_apify(NAUKRI_RUN_URL, payload)
    except Exception as e:
        return handle_error(e)

    if not jobs:
        return (
            f"No job found with ID '{params.job_id}'. "
            "Double-check the ID from naukri_search_jobs."
        )

    job = jobs[0]
    details = job.get("jobDetails", {}) or {}
    company = details.get("companyDetail", {}) or {}
    skills = details.get("keySkills", {}) or {}
    salary = details.get("salaryDetail", {}) or {}
    ambition = job.get("ambitionBoxDetails", {}) or {}
    reviews = ambition.get("reviews", []) or []

    emails = []
    for field in ["recruiterEmail", "email", "contactEmail", "hrEmail"]:
        if job.get(field):
            emails.append(str(job[field]))

    desc = details.get("description", "") or ""
    emails.extend(extract_emails(desc))
    emails.extend(extract_emails(company.get("details", "") or ""))

    clean_desc = re.sub(r"<[^>]+>", " ", desc)
    clean_desc = clean_desc.replace("&nbsp;", " ").replace("&amp;", "&").strip()

    lines = [
        f"# {details.get('title', job.get('title', 'N/A'))}",
        f"**Company**: {company.get('name', job.get('companyName', 'N/A'))}",
        f"**Location**: {', '.join(loc.get('label', '') for loc in details.get('locations', []) or [])}",
        f"**Experience**: {details.get('experienceText', 'N/A')}",
        f"**Employment Type**: {details.get('employmentType', 'N/A')}",
        f"**Salary**: {salary.get('label', 'Not disclosed')}",
        f"**Work Mode**: {details.get('wfhLabel', 'Office')}",
        "",
        "## Required Skills",
        ", ".join(
            s.get("label", "")
            for s in (skills.get("preferred", []) or []) + (skills.get("other", []) or [])
        ),
        "",
        "## About the Company",
        company.get("details", "N/A") or "N/A",
        "",
        "## Job Description",
        clean_desc[:3000] if clean_desc else "N/A",
    ]

    if emails:
        lines.append("\n## Contact / HR Emails found")
        lines.append(", ".join(sorted(set(emails))))
    else:
        lines.append("\n## Contact / HR Emails")
        lines.append("_No public emails found in this posting (common on Naukri India)._")

    if reviews:
        lines.append("\n## Employee Reviews (via AmbitionBox)")
        for r in reviews[:2]:
            lines.append(
                f"- **{r.get('Title', '')}**: {str(r.get('LikesText', ''))[:200]}..."
            )

    return "\n".join(lines)


# ─── Tool 3: LinkedIn Search (last 24 hours + recruiter info) ─────────────────

class LinkedInSearchInput(BaseModel):
    keyword: str = Field(
        ...,
        description="Job title or skill (e.g. 'software engineer', 'data analyst')",
        min_length=1,
        max_length=100,
    )
    location: Optional[str] = Field(
        default="India",
        description="Location (e.g. 'Chennai', 'Coimbatore', 'Madurai', 'India', 'Remote')",
    )
    max_jobs: int = Field(
        default=10,
        description=(
            "Maximum number of jobs to return (1-30). "
            "Keep low because detailed scrape costs more."
        ),
        ge=1,
        le=30,
    )


@mcp.tool(
    name="linkedin_search_jobs",
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def linkedin_search_jobs(params: LinkedInSearchInput) -> str:
    """
    Search LinkedIn for FRESH jobs posted in the LAST 24 HOURS.
    Tries to extract job poster / recruiter name and any emails found in the description.
    Note: LinkedIn almost never shows recruiter emails on public pages.
    """
    search_url = (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={params.keyword.replace(' ', '%20')}"
        f"&location={(params.location or 'India').replace(' ', '%20')}"
        f"&f_TPR=r86400"
        f"&sortBy=DD"
    )

    payload = {
        "urls": [search_url],
        "maxItems": params.max_jobs,
        "scrapeJobDetails": True,
    }

    try:
        jobs = await call_apify(LINKEDIN_RUN_URL, payload)
    except Exception as e:
        return handle_error(e)

    if not jobs:
        return (
            f"No LinkedIn jobs found for '{params.keyword}' "
            f"in {params.location or 'India'} (last 24 hours)."
        )

    lines = [
        f"Found {len(jobs)} **fresh (last 24h)** LinkedIn jobs "
        f"for '{params.keyword}':\n"
    ]

    for job in jobs[: params.max_jobs]:
        title = job.get("title") or job.get("jobTitle") or "N/A"
        company = job.get("companyName") or job.get("company") or "N/A"
        loc = job.get("location") or job.get("jobLocation") or "N/A"
        posted = (
            job.get("postedAt")
            or job.get("postedDate")
            or job.get("postedTime")
            or "N/A"
        )
        url = job.get("jobUrl") or job.get("url") or job.get("link") or ""

        poster_name = (
            job.get("jobPosterName")
            or job.get("posterName")
            or job.get("recruiterName")
            or job.get("hiringManagerName")
            or ""
        )
        poster_title = (
            job.get("jobPosterTitle")
            or job.get("posterTitle")
            or job.get("recruiterTitle")
            or ""
        )
        company_url = (
            job.get("companyUrl") or job.get("companyLinkedinUrl") or ""
        )

        description = (
            job.get("description")
            or job.get("jobDescription")
            or job.get("descriptionText")
            or ""
        )
        emails = extract_emails(description)

        block = [
            f"**{title}** at {company}",
            f"  Location     : {loc}",
            f"  Posted       : {posted}",
            f"  Job URL      : {url}",
        ]

        if company_url:
            block.append(f"  Company page : {company_url}")

        if poster_name or poster_title:
            if poster_name and poster_title:
                poster = f"{poster_name} ({poster_title})"
            else:
                poster = poster_name or poster_title
            block.append(f"  Job poster   : {poster}")
        else:
            block.append("  Job poster   : Not available")

        if emails:
            block.append(f"  Emails found : {', '.join(sorted(set(emails)))}")
        else:
            block.append(
                "  Emails found : None (normal on LinkedIn public pages)"
            )

        lines.append("\n".join(block))
        lines.append("---")

    lines.append(
        "\n_Note: LinkedIn almost never exposes recruiter emails publicly. "
        "Use the job poster name + company page to find them on LinkedIn "
        "or the company website._"
    )

    return "\n".join(lines)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
