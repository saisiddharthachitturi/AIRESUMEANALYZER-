from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for

try:
    from PyPDF2 import PdfReader
except Exception:  # pragma: no cover - app still supports TXT/DOCX without PyPDF2
    PdfReader = None


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 40 * 1024 * 1024


SKILL_SYNONYMS = {
    "AI": "Artificial Intelligence",
    "Artificial Intelligence": "Artificial Intelligence",
    "ML": "Machine Learning",
    "Machine Learning": "Machine Learning",
    "NLP": "Natural Language Processing",
    "Natural Language Processing": "Natural Language Processing",
    "JS": "JavaScript",
    "Javascript": "JavaScript",
    "JavaScript": "JavaScript",
    "React.js": "React",
    "ReactJS": "React",
    "Node.js": "Node.js",
    "NodeJS": "Node.js",
    "PostgreSQL": "PostgreSQL",
    "Postgres": "PostgreSQL",
    "MS SQL": "SQL",
    "SQL Server": "SQL",
    "CI/CD": "CI/CD",
    "CICD": "CI/CD",
    "K8s": "Kubernetes",
}

CANONICAL_SKILLS = sorted(
    {
        "Python",
        "Java",
        "C++",
        "C#",
        "Go",
        "Ruby",
        "PHP",
        "Scala",
        "R",
        "SQL",
        "NoSQL",
        "PostgreSQL",
        "MySQL",
        "MongoDB",
        "Redis",
        "Excel",
        "Power BI",
        "Tableau",
        "JavaScript",
        "TypeScript",
        "HTML",
        "CSS",
        "React",
        "Angular",
        "Vue",
        "Node.js",
        "Django",
        "Flask",
        "FastAPI",
        "Spring",
        "REST",
        "GraphQL",
        "AWS",
        "Azure",
        "GCP",
        "Docker",
        "Kubernetes",
        "Terraform",
        "Linux",
        "Git",
        "CI/CD",
        "Jenkins",
        "Data Analysis",
        "Data Science",
        "Machine Learning",
        "Deep Learning",
        "Natural Language Processing",
        "Computer Vision",
        "TensorFlow",
        "PyTorch",
        "Scikit-learn",
        "Pandas",
        "NumPy",
        "Matplotlib",
        "Spark",
        "Hadoop",
        "ETL",
        "Airflow",
        "Statistics",
        "Agile",
        "Scrum",
        "Product Management",
        "Project Management",
        "Communication",
        "Leadership",
        "Problem Solving",
        "System Design",
        "Microservices",
        "Cybersecurity",
        "Testing",
        "Selenium",
        "API",
    }
    | set(SKILL_SYNONYMS.values())
)

DEGREES = {
    "phd": 5,
    "doctorate": 5,
    "m.tech": 4,
    "master": 4,
    "mba": 4,
    "m.sc": 4,
    "ms": 4,
    "b.tech": 3,
    "bachelor": 3,
    "b.sc": 3,
    "bs": 3,
    "be": 3,
    "diploma": 2,
}

SECTION_ALIASES = {
    "education": ["education", "academic background", "qualification"],
    "experience": ["experience", "work experience", "employment", "professional experience"],
    "projects": ["projects", "selected projects", "academic projects"],
    "certifications": ["certifications", "certificates", "licenses"],
    "skills": ["skills", "technical skills", "core competencies"],
}

JOBS: dict[str, dict[str, Any]] = {}


@dataclass
class JDRequirements:
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    experience_years: int = 0
    education: list[str] = field(default_factory=list)
    source_file: str = ""


@dataclass
class Candidate:
    rank: int
    name: str
    email: str
    phone: str
    file_name: str
    skills: list[str]
    education: str
    experience: str
    experience_years: int
    projects: str
    certifications: str
    score: int
    score_breakdown: dict[str, int]
    matched_skills: list[str]
    missing_skills: list[str]
    strengths: list[str]
    summary: str
    interview_focus: list[str]


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_text_from_upload(file_name: str, file_bytes: bytes) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".txt":
        return file_bytes.decode("utf-8", errors="ignore")
    if suffix == ".pdf":
        if PdfReader is None:
            return ""
        reader = PdfReader(io.BytesIO(file_bytes))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if suffix == ".docx":
        return extract_docx_text(file_bytes)
    return ""


def extract_docx_text(file_bytes: bytes) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as docx:
        for name in docx.namelist():
            if name.startswith("word/") and name.endswith(".xml"):
                try:
                    root = ElementTree.fromstring(docx.read(name))
                except ElementTree.ParseError:
                    continue
                for node in root.iter():
                    if node.text and node.tag.endswith("}t"):
                        parts.append(node.text)
                    elif node.tag.endswith("}p"):
                        parts.append("\n")
    return " ".join(parts)


def skill_pattern(skill: str) -> re.Pattern[str]:
    escaped = re.escape(skill).replace(r"\ ", r"[\s\-]+")
    if skill in {"C++", "C#", "R"}:
        return re.compile(rf"(?<![A-Za-z0-9+.#]){escaped}(?![A-Za-z0-9+.#])", re.I)
    return re.compile(rf"\b{escaped}\b", re.I)


def canonicalize_skill(skill: str) -> str:
    for alias, canonical in SKILL_SYNONYMS.items():
        if alias.lower() == skill.lower():
            return canonical
    for canonical in CANONICAL_SKILLS:
        if canonical.lower() == skill.lower():
            return canonical
    return skill.title()


def extract_skills(text: str) -> list[str]:
    found = set()
    searchable = text.replace("/", " / ")
    for alias, canonical in SKILL_SYNONYMS.items():
        if skill_pattern(alias).search(searchable):
            found.add(canonical)
    for skill in CANONICAL_SKILLS:
        if skill_pattern(skill).search(searchable):
            found.add(canonicalize_skill(skill))
    return sorted(found)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def extract_years(text: str) -> int:
    years = [int(y) for y in re.findall(r"(\d{1,2})\+?\s*(?:years?|yrs?)", text, re.I)]
    return max(years) if years else 0


def extract_education_requirements(text: str) -> list[str]:
    lower = text.lower()
    matches = [degree for degree in DEGREES if re.search(rf"\b{re.escape(degree)}\b", lower)]
    return sorted(set(matches), key=lambda d: DEGREES[d], reverse=True)


def parse_jd(text: str, file_name: str) -> JDRequirements:
    all_skills = extract_skills(text)
    required, preferred = set(), set()
    for sentence in split_sentences(text):
        sentence_skills = extract_skills(sentence)
        if not sentence_skills:
            continue
        lower = sentence.lower()
        if any(word in lower for word in ["required", "must", "mandatory", "need", "minimum"]):
            required.update(sentence_skills)
        elif any(word in lower for word in ["preferred", "nice to have", "plus", "good to have", "bonus"]):
            preferred.update(sentence_skills)
    if not required:
        required.update(all_skills)
    preferred.difference_update(required)
    return JDRequirements(
        required_skills=sorted(required),
        preferred_skills=sorted(preferred),
        experience_years=extract_years(text),
        education=extract_education_requirements(text),
        source_file=file_name,
    )


def extract_section(text: str, section: str) -> str:
    aliases = SECTION_ALIASES[section]
    all_headers = [item for values in SECTION_ALIASES.values() for item in values]
    header_regex = "|".join(re.escape(h) for h in all_headers)
    for alias in aliases:
        pattern = re.compile(
            rf"(?:^|\n)\s*{re.escape(alias)}\s*:?\s*\n?(.*?)(?=\n\s*(?:{header_regex})\s*:?\s*\n|$)",
            re.I | re.S,
        )
        match = pattern.search(text)
        if match:
            return normalize_spaces(match.group(1))[:1500]
    return ""


def extract_name(text: str, email: str) -> str:
    lines = [normalize_spaces(line) for line in text.splitlines() if normalize_spaces(line)]
    blocked = {"resume", "curriculum vitae", "cv", "profile", "summary"}
    for line in lines[:8]:
        if email and email in line:
            continue
        if any(token in line.lower() for token in ["@", "phone", "email", "linkedin", "github"]):
            continue
        words = re.findall(r"[A-Za-z][A-Za-z.'-]*", line)
        if 2 <= len(words) <= 4 and line.lower() not in blocked:
            return " ".join(word.capitalize() for word in words)
    return "Unknown Candidate"


def parse_resume(text: str, file_name: str, jd: JDRequirements) -> Candidate:
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    phone_match = re.search(r"(?:\+?\d[\d\s().-]{8,}\d)", text)
    email = email_match.group(0) if email_match else ""
    phone = normalize_spaces(phone_match.group(0)) if phone_match else ""
    skills = extract_skills(text)
    experience_years = extract_years(text)
    education = extract_section(text, "education") or summarize_education(text)
    experience = extract_section(text, "experience") or summarize_experience(text)
    projects = extract_section(text, "projects")
    certifications = extract_section(text, "certifications")
    score, breakdown, matched, missing = score_candidate(skills, experience_years, education, jd)
    strengths, summary, focus = generate_insights(matched, missing, experience_years, jd, breakdown)
    return Candidate(
        rank=0,
        name=extract_name(text, email),
        email=email,
        phone=phone,
        file_name=file_name,
        skills=skills,
        education=education,
        experience=experience,
        experience_years=experience_years,
        projects=projects,
        certifications=certifications,
        score=score,
        score_breakdown=breakdown,
        matched_skills=matched,
        missing_skills=missing,
        strengths=strengths,
        summary=summary,
        interview_focus=focus,
    )


def summarize_education(text: str) -> str:
    sentences = [s for s in split_sentences(text) if any(degree in s.lower() for degree in DEGREES)]
    return normalize_spaces(" ".join(sentences[:3]))[:1000]


def summarize_experience(text: str) -> str:
    sentences = [s for s in split_sentences(text) if re.search(r"\b(company|developer|engineer|analyst|manager|intern|worked|built|led)\b", s, re.I)]
    return normalize_spaces(" ".join(sentences[:5]))[:1200]


def score_candidate(
    candidate_skills: list[str],
    experience_years: int,
    education_text: str,
    jd: JDRequirements,
) -> tuple[int, dict[str, int], list[str], list[str]]:
    required = set(jd.required_skills)
    preferred = set(jd.preferred_skills)
    candidate = set(candidate_skills)
    required_matched = required & candidate
    preferred_matched = preferred & candidate
    total_weight = (len(required) * 2) + len(preferred)
    matched_weight = (len(required_matched) * 2) + len(preferred_matched)
    skill_score = int((matched_weight / total_weight) * 100) if total_weight else 100
    experience_score = 100 if not jd.experience_years else min(100, int((experience_years / jd.experience_years) * 100))
    education_score = score_education(education_text, jd.education)
    final = round((skill_score * 0.60) + (experience_score * 0.25) + (education_score * 0.15))
    missing = sorted(required - candidate)
    matched = sorted((required | preferred) & candidate)
    return final, {
        "Skill Match": skill_score,
        "Experience Match": experience_score,
        "Education Match": education_score,
    }, matched, missing


def score_education(education_text: str, required_education: list[str]) -> int:
    if not required_education:
        return 100
    lower = education_text.lower()
    candidate_level = max([DEGREES[d] for d in DEGREES if re.search(rf"\b{re.escape(d)}\b", lower)] or [0])
    required_level = max(DEGREES.get(d, 0) for d in required_education)
    if candidate_level >= required_level:
        return 100
    if candidate_level and candidate_level == required_level - 1:
        return 70
    return 35 if candidate_level else 20


def generate_insights(
    matched: list[str],
    missing: list[str],
    experience_years: int,
    jd: JDRequirements,
    breakdown: dict[str, int],
) -> tuple[list[str], str, list[str]]:
    strengths = []
    if matched:
        strengths.append(f"Matches key requirements in {', '.join(matched[:6])}.")
    if experience_years:
        strengths.append(f"Shows approximately {experience_years} years of relevant experience.")
    if breakdown["Education Match"] >= 90:
        strengths.append("Education appears aligned with the role requirements.")
    if not strengths:
        strengths.append("Has a readable profile but limited direct requirement overlap was detected.")

    if breakdown["Skill Match"] >= 80 and breakdown["Experience Match"] >= 80:
        summary = "Strong fit for the role based on skills and experience alignment."
    elif breakdown["Skill Match"] >= 55:
        summary = "Potential fit with several relevant skills; validate depth during screening."
    else:
        summary = "Lower fit based on the uploaded JD; consider only if adjacent experience is valuable."

    focus = []
    if missing:
        focus.append(f"Probe gaps around {', '.join(missing[:5])}.")
    if jd.experience_years and experience_years < jd.experience_years:
        focus.append("Validate whether project depth compensates for the experience requirement.")
    focus.append("Ask for concrete examples of recent work matching the JD responsibilities.")
    return strengths, summary, focus


def analyze_job(job_id: str, jd_file: tuple[str, bytes], resumes: list[tuple[str, bytes]]) -> None:
    job = JOBS[job_id]
    try:
        job["status"] = "processing"
        job["message"] = "Reading job description"
        jd_text = extract_text_from_upload(*jd_file)
        jd = parse_jd(jd_text, jd_file[0])
        job["jd"] = jd
        candidates: list[Candidate] = []
        total = max(len(resumes), 1)
        for index, resume in enumerate(resumes, start=1):
            job["message"] = f"Analyzing {resume[0]}"
            resume_text = extract_text_from_upload(*resume)
            candidates.append(parse_resume(resume_text, resume[0], jd))
            job["progress"] = int((index / total) * 100)
            time.sleep(0.15)
        candidates.sort(key=lambda item: item.score, reverse=True)
        for rank, candidate in enumerate(candidates, start=1):
            candidate.rank = rank
        job["candidates"] = candidates
        job["progress"] = 100
        job["message"] = "Analysis complete"
        job["status"] = "complete"
    except Exception as exc:
        job["status"] = "error"
        job["message"] = str(exc)


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    data = candidate.__dict__.copy()
    return data


BASE_CSS = """
<style>
:root {
  --bg: #f5f7fb;
  --panel: #ffffff;
  --ink: #172033;
  --muted: #667085;
  --line: #d9e1ee;
  --brand: #2563eb;
  --brand-dark: #1d4ed8;
  --good: #0f9f6e;
  --warn: #c27b00;
  --bad: #c2410c;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: Inter, Segoe UI, Roboto, Arial, sans-serif;
}
a { color: var(--brand); text-decoration: none; }
.shell { max-width: 1180px; margin: 0 auto; padding: 28px 20px 44px; }
.topbar { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 22px; }
.brand h1 { margin: 0; font-size: 32px; letter-spacing: 0; }
.brand p { margin: 6px 0 0; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; }
.panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 12px 28px rgba(23, 32, 51, 0.06);
}
.panel h2 { margin: 0 0 12px; font-size: 18px; }
.dropzone {
  min-height: 160px;
  border: 2px dashed #a9b8cf;
  border-radius: 8px;
  display: grid;
  place-items: center;
  text-align: center;
  padding: 24px;
  background: #f9fbff;
  transition: border-color .2s, background .2s;
}
.dropzone.dragover { border-color: var(--brand); background: #eff6ff; }
.dropzone strong { display: block; margin-bottom: 8px; }
.dropzone span, .hint { color: var(--muted); font-size: 14px; }
textarea {
  width: 100%;
  min-height: 160px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 12px;
  resize: vertical;
  font: inherit;
  color: var(--ink);
  background: #ffffff;
}
.or-divider {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--muted);
  font-size: 13px;
  margin: 14px 0;
}
.or-divider::before, .or-divider::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--line);
}
input[type=file] { display: none; }
.file-meta { margin-top: 12px; color: var(--muted); font-size: 14px; min-height: 22px; }
.actions { display: flex; align-items: center; gap: 14px; margin-top: 20px; }
button, .button {
  border: 0;
  background: var(--brand);
  color: white;
  border-radius: 8px;
  padding: 12px 18px;
  font-weight: 700;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 42px;
}
button:hover, .button:hover { background: var(--brand-dark); color: white; }
button:disabled { background: #98a2b3; cursor: wait; }
.progress-wrap { display: none; flex: 1; min-width: 220px; }
.progress-text { color: var(--muted); font-size: 14px; margin-bottom: 7px; }
.progress { height: 10px; border-radius: 999px; background: #e4eaf3; overflow: hidden; }
.bar { width: 0; height: 100%; background: linear-gradient(90deg, var(--brand), var(--good)); transition: width .25s; }
.toolbar { display: grid; grid-template-columns: 2fr 1fr 1fr 1fr auto; gap: 10px; margin: 18px 0; }
.toolbar input, .toolbar select {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 11px 12px;
  background: white;
}
table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { padding: 13px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 14px; }
th { background: #eef3fb; cursor: pointer; user-select: none; color: #344054; }
tr:last-child td { border-bottom: 0; }
.pill { display: inline-block; padding: 4px 8px; border-radius: 999px; background: #eef3fb; color: #344054; margin: 2px; font-size: 12px; }
.score { font-weight: 800; }
.score.good { color: var(--good); }
.score.mid { color: var(--warn); }
.score.low { color: var(--bad); }
.stat-row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
.stat { background: white; border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
.stat b { display: block; font-size: 24px; margin-bottom: 4px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }
.stack { display: grid; gap: 18px; }
.breakdown { display: grid; gap: 12px; }
.metric { display: grid; grid-template-columns: 150px 1fr 44px; align-items: center; gap: 10px; }
.metric-track { height: 9px; background: #e4eaf3; border-radius: 999px; overflow: hidden; }
.metric-fill { height: 100%; background: var(--brand); }
.empty { padding: 30px; text-align: center; color: var(--muted); }
@media (max-width: 820px) {
  .grid, .detail-grid, .stat-row, .toolbar { grid-template-columns: 1fr; }
  .topbar { align-items: flex-start; flex-direction: column; }
  table { display: block; overflow-x: auto; }
}
</style>
"""


INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Resume Analyzer</title>
  """ + BASE_CSS + """
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div class="brand">
        <h1>AI Resume Analyzer</h1>
        <p>Paste or upload a job description, then upload all resumes at once to rank candidates by role fit.</p>
      </div>
    </section>

    <form id="analyzeForm" enctype="multipart/form-data">
      <section class="grid">
        <div class="panel">
          <h2>Job Description</h2>
          <textarea id="jdText" name="jd_text" placeholder="Paste the job description here, or upload a JD file below."></textarea>
          <div class="or-divider">or upload a file</div>
          <div class="dropzone" data-input="jdFile">
            <div>
              <strong>Upload JD file</strong>
              <span>PDF, DOCX, or TXT</span>
            </div>
          </div>
          <input id="jdFile" name="jd" type="file" accept=".pdf,.docx,.txt">
          <div id="jdMeta" class="file-meta">No job description file selected</div>
        </div>

        <label class="panel">
          <h2>Resumes</h2>
          <div class="dropzone" data-input="resumeFiles">
            <div>
              <strong>Drop all resumes here</strong>
              <span>Select or drag multiple PDF/DOCX resumes in one upload</span>
            </div>
          </div>
          <input id="resumeFiles" name="resumes" type="file" accept=".pdf,.docx" multiple required>
          <div id="resumeMeta" class="file-meta">0 resumes selected</div>
        </label>
      </section>

      <section class="actions">
        <button id="analyzeBtn" type="submit">Analyze Resumes</button>
        <div id="progressWrap" class="progress-wrap">
          <div id="progressText" class="progress-text">Preparing analysis</div>
          <div class="progress"><div id="progressBar" class="bar"></div></div>
        </div>
      </section>
    </form>
  </main>

  <script>
    const jdFile = document.getElementById("jdFile");
    const jdText = document.getElementById("jdText");
    const resumeFiles = document.getElementById("resumeFiles");
    const jdMeta = document.getElementById("jdMeta");
    const resumeMeta = document.getElementById("resumeMeta");
    const progressWrap = document.getElementById("progressWrap");
    const progressBar = document.getElementById("progressBar");
    const progressText = document.getElementById("progressText");
    const analyzeBtn = document.getElementById("analyzeBtn");

    function updateMeta() {
      const pasted = jdText.value.trim().length;
      jdMeta.textContent = jdFile.files.length ? jdFile.files[0].name : "No job description file selected";
      if (pasted) jdMeta.textContent += ` - pasted JD text detected`;
      resumeMeta.textContent = `${resumeFiles.files.length} resume${resumeFiles.files.length === 1 ? "" : "s"} selected`;
    }
    jdFile.addEventListener("change", updateMeta);
    jdText.addEventListener("input", updateMeta);
    resumeFiles.addEventListener("change", updateMeta);

    document.querySelectorAll(".dropzone").forEach(zone => {
      const input = document.getElementById(zone.dataset.input);
      zone.addEventListener("click", event => { event.preventDefault(); input.click(); });
      zone.addEventListener("dragover", event => { event.preventDefault(); zone.classList.add("dragover"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));
      zone.addEventListener("drop", event => {
        event.preventDefault();
        zone.classList.remove("dragover");
        input.files = event.dataTransfer.files;
        updateMeta();
      });
    });

    document.getElementById("analyzeForm").addEventListener("submit", async event => {
      event.preventDefault();
      analyzeBtn.disabled = true;
      progressWrap.style.display = "block";
      progressText.textContent = "Uploading files";
      progressBar.style.width = "8%";
      const response = await fetch("/api/analyze", { method: "POST", body: new FormData(event.target) });
      const data = await response.json();
      if (!response.ok) {
        progressText.textContent = data.error || "Unable to start analysis";
        analyzeBtn.disabled = false;
        return;
      }
      const timer = setInterval(async () => {
        const progress = await fetch(`/api/progress/${data.job_id}`).then(r => r.json());
        progressBar.style.width = `${progress.progress || 0}%`;
        progressText.textContent = progress.message || "Processing resumes";
        if (progress.status === "complete") {
          clearInterval(timer);
          window.location.href = `/results/${data.job_id}`;
        }
        if (progress.status === "error") {
          clearInterval(timer);
          analyzeBtn.disabled = false;
        }
      }, 450);
    });
  </script>
</body>
</html>
"""


RESULTS_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Resume Analysis Results</title>
  """ + BASE_CSS + """
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div class="brand">
        <h1>Ranked Candidates</h1>
        <p>Results for {{ jd.source_file }} - Required skills: {{ jd.required_skills | join(", ") or "Not detected" }}</p>
      </div>
      <a class="button" href="{{ url_for('index') }}">New Analysis</a>
    </section>

    <section class="stat-row">
      <div class="stat"><b>{{ candidates|length }}</b><span class="hint">Candidates analyzed</span></div>
      <div class="stat"><b>{{ top_score }}</b><span class="hint">Top match score</span></div>
      <div class="stat"><b>{{ jd.experience_years }}+</b><span class="hint">JD experience target</span></div>
      <div class="stat"><b>{{ jd.required_skills|length }}</b><span class="hint">Required skills found in JD</span></div>
    </section>

    <section class="panel">
      <h2>Results Dashboard</h2>
      <div class="toolbar">
        <input id="search" placeholder="Search candidate or file">
        <input id="scoreFilter" type="number" min="0" max="100" placeholder="Minimum score">
        <input id="skillFilter" placeholder="Filter by skill">
        <input id="expFilter" type="number" min="0" placeholder="Minimum years">
        <a class="button" href="{{ url_for('download_csv', job_id=job_id) }}">Download CSV</a>
      </div>
      <table id="candidateTable">
        <thead>
          <tr>
            <th data-type="number">Rank</th>
            <th>Candidate Name</th>
            <th data-type="number">Match Score</th>
            <th data-type="number">Experience</th>
            <th>Skills Found</th>
            <th>Missing Skills</th>
            <th>Resume File Name</th>
            <th>Profile</th>
          </tr>
        </thead>
        <tbody>
          {% for c in candidates %}
          <tr data-search="{{ (c.name ~ ' ' ~ c.file_name)|lower }}" data-score="{{ c.score }}" data-skills="{{ c.skills|join(' ')|lower }}" data-exp="{{ c.experience_years }}">
            <td>{{ c.rank }}</td>
            <td>{{ c.name }}</td>
            <td><span class="score {{ 'good' if c.score >= 75 else 'mid' if c.score >= 50 else 'low' }}">{{ c.score }}%</span></td>
            <td>{{ c.experience_years }} years</td>
            <td>{% for skill in c.matched_skills[:8] %}<span class="pill">{{ skill }}</span>{% endfor %}</td>
            <td>{% for skill in c.missing_skills[:8] %}<span class="pill">{{ skill }}</span>{% else %}<span class="pill">None</span>{% endfor %}</td>
            <td>{{ c.file_name }}</td>
            <td><a href="{{ url_for('candidate_detail', job_id=job_id, rank=c.rank) }}">View</a></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </section>
  </main>

  <script>
    const table = document.getElementById("candidateTable");
    const tbody = table.querySelector("tbody");
    const inputs = ["search", "scoreFilter", "skillFilter", "expFilter"].map(id => document.getElementById(id));

    function applyFilters() {
      const search = document.getElementById("search").value.toLowerCase();
      const minScore = Number(document.getElementById("scoreFilter").value || 0);
      const skill = document.getElementById("skillFilter").value.toLowerCase();
      const minExp = Number(document.getElementById("expFilter").value || 0);
      tbody.querySelectorAll("tr").forEach(row => {
        const visible =
          row.dataset.search.includes(search) &&
          Number(row.dataset.score) >= minScore &&
          row.dataset.skills.includes(skill) &&
          Number(row.dataset.exp) >= minExp;
        row.style.display = visible ? "" : "none";
      });
    }
    inputs.forEach(input => input.addEventListener("input", applyFilters));

    table.querySelectorAll("th").forEach((th, index) => {
      th.addEventListener("click", () => {
        const rows = Array.from(tbody.querySelectorAll("tr"));
        const numeric = th.dataset.type === "number";
        const direction = th.dataset.direction === "asc" ? -1 : 1;
        th.dataset.direction = direction === 1 ? "asc" : "desc";
        rows.sort((a, b) => {
          const av = a.children[index].innerText.replace("%", "");
          const bv = b.children[index].innerText.replace("%", "");
          return numeric ? (Number(av) - Number(bv)) * direction : av.localeCompare(bv) * direction;
        });
        rows.forEach(row => tbody.appendChild(row));
      });
    });
  </script>
</body>
</html>
"""


DETAIL_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ c.name }} - Candidate Profile</title>
  """ + BASE_CSS + """
</head>
<body>
  <main class="shell">
    <section class="topbar">
      <div class="brand">
        <h1>{{ c.name }}</h1>
        <p>{{ c.file_name }} - {{ c.email or "No email detected" }} - {{ c.phone or "No phone detected" }}</p>
      </div>
      <a class="button" href="{{ url_for('results', job_id=job_id) }}">Back to Dashboard</a>
    </section>

    <section class="stat-row">
      <div class="stat"><b>#{{ c.rank }}</b><span class="hint">Rank</span></div>
      <div class="stat"><b>{{ c.score }}%</b><span class="hint">Overall match</span></div>
      <div class="stat"><b>{{ c.experience_years }}</b><span class="hint">Years detected</span></div>
      <div class="stat"><b>{{ c.skills|length }}</b><span class="hint">Skills extracted</span></div>
    </section>

    <section class="detail-grid">
      <div class="stack">
        <div class="panel">
          <h2>Candidate Summary</h2>
          <p>{{ c.summary }}</p>
          {% for item in c.strengths %}<p>&bull; {{ item }}</p>{% endfor %}
        </div>
        <div class="panel">
          <h2>Extracted Skills</h2>
          {% for skill in c.skills %}<span class="pill">{{ skill }}</span>{% else %}<p class="hint">No skills detected.</p>{% endfor %}
        </div>
        <div class="panel">
          <h2>Missing Skills Compared to JD</h2>
          {% for skill in c.missing_skills %}<span class="pill">{{ skill }}</span>{% else %}<p class="hint">No required skill gaps detected.</p>{% endfor %}
        </div>
      </div>

      <div class="stack">
        <div class="panel">
          <h2>Matching Score Breakdown</h2>
          <div class="breakdown">
          {% for label, value in c.score_breakdown.items() %}
            <div class="metric">
              <span>{{ label }}</span>
              <div class="metric-track"><div class="metric-fill" style="width: {{ value }}%"></div></div>
              <strong>{{ value }}%</strong>
            </div>
          {% endfor %}
          </div>
        </div>
        <div class="panel">
          <h2>Interview Focus Areas</h2>
          {% for item in c.interview_focus %}<p>&bull; {{ item }}</p>{% endfor %}
        </div>
      </div>
    </section>

    <section class="detail-grid" style="margin-top:18px">
      <div class="panel"><h2>Education</h2><p>{{ c.education or "No education section detected." }}</p></div>
      <div class="panel"><h2>Experience</h2><p>{{ c.experience or "No experience section detected." }}</p></div>
      <div class="panel"><h2>Projects</h2><p>{{ c.projects or "No projects section detected." }}</p></div>
      <div class="panel"><h2>Certifications</h2><p>{{ c.certifications or "No certifications section detected." }}</p></div>
    </section>
  </main>
</body>
</html>
"""


@app.route("/")
def index() -> str:
    return render_template_string(INDEX_HTML)


@app.post("/api/analyze")
def api_analyze() -> Response:
    jd_upload = request.files.get("jd")
    jd_text = request.form.get("jd_text", "").strip()
    resume_uploads = request.files.getlist("resumes")
    if not jd_text and (not jd_upload or not jd_upload.filename):
        return jsonify({"error": "Please paste a job description or upload a JD file."}), 400
    if not resume_uploads or not any(file.filename for file in resume_uploads):
        return jsonify({"error": "Please upload at least one resume."}), 400

    if jd_text:
        jd_file = ("Pasted Job Description.txt", jd_text.encode("utf-8"))
    else:
        jd_file = (jd_upload.filename, jd_upload.read())
    resumes = [(file.filename, file.read()) for file in resume_uploads if file.filename]
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"status": "queued", "progress": 0, "message": "Queued", "candidates": [], "jd": None}
    thread = threading.Thread(target=analyze_job, args=(job_id, jd_file, resumes), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.get("/api/progress/<job_id>")
def api_progress(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"status": "missing", "progress": 0, "message": "Analysis not found"}), 404
    return jsonify({"status": job["status"], "progress": job["progress"], "message": job["message"]})


@app.get("/results/<job_id>")
def results(job_id: str) -> str:
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        return redirect(url_for("index"))
    candidates = job["candidates"]
    top_score = f"{candidates[0].score}%" if candidates else "0%"
    return render_template_string(RESULTS_HTML, job_id=job_id, jd=job["jd"], candidates=candidates, top_score=top_score)


@app.get("/candidate/<job_id>/<int:rank>")
def candidate_detail(job_id: str, rank: int) -> str:
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        return redirect(url_for("index"))
    candidate = next((c for c in job["candidates"] if c.rank == rank), None)
    if not candidate:
        return redirect(url_for("results", job_id=job_id))
    return render_template_string(DETAIL_HTML, job_id=job_id, c=candidate)


@app.get("/download/<job_id>.csv")
def download_csv(job_id: str) -> Response:
    job = JOBS.get(job_id)
    if not job or job.get("status") != "complete":
        return Response("Analysis not found", status=404)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Rank", "Candidate Name", "Match Score", "Experience", "Skills Found", "Missing Skills", "Resume File Name"])
    for candidate in job["candidates"]:
        writer.writerow([
            candidate.rank,
            candidate.name,
            candidate.score,
            candidate.experience_years,
            ", ".join(candidate.matched_skills),
            ", ".join(candidate.missing_skills),
            candidate.file_name,
        ])
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=resume_analysis_results.csv"},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
