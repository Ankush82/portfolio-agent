"""Temporary verification script for raw-SQL check logic."""
import re, ast
from pathlib import Path

SRC = Path("src")

def collect(src):
    out = []
    for node in ast.walk(ast.parse(src)):
        match node:
            case ast.Import(names=n): out += [a.name.split('.')[0] for a in n]
            case ast.ImportFrom(module=m, names=_): out.append(m.split('.')[0]) if m else None
    return out

# Match Python string literals (handles triple-quoted, raw, f, etc.)
_STRING_RE = re.compile(
    r"(?:r|rb|br|f|fr|rf|br|rb)?"
    r"('''[^'\\]*(?:\\.[^'\\]*)*'''|"   # single-quoted triple
    r'"""[^"\\]*(?:\\.[^"\\]*)*"""|'     # double-quoted triple
    r"'[^'\\]*(?:\\.[^'\\]*)*'|"          # single-quoted single
    r'"[^"\\]*(?:\\.[^"\\]*)*")',          # double-quoted single
    re.DOTALL
)

def extract_strings(src):
    return "\n".join(_STRING_RE.findall(src))

violations = []
for fp in sorted((SRC / "repositories").rglob("*.py")):
    if fp.name == "__init__.py":
        continue
    src = fp.read_text()
    for imp in collect(src):
        if imp in ("psycopg", "redis", "sqlalchemy", "infrastructure_postgres"):
            violations.append(f"{fp}: forbidden import: {imp}")
    strings = extract_strings(src)
    for pat in [
        re.compile(r"\bSELECT\b", re.I),
        re.compile(r"\bINSERT\b", re.I),
        re.compile(r"\bUPDATE\b", re.I),
        re.compile(r"\bDELETE\s+FROM\b", re.I),
    ]:
        if pat.search(strings):
            violations.append(f"{fp}: raw SQL string")

print("REPO violations:", violations or "NONE")

src = (SRC / "domain.py").read_text()
non_std = [i for i in collect(src) if i not in ("dataclasses", "decimal", "re", "typing", "types")]
print("DOMAIN non-stdlib:", non_std or "NONE")

for fp in sorted((SRC / "components").rglob("*.py")):
    if fp.name == "__init__.py":
        continue
    for imp in collect(fp.read_text()):
        if imp in ("psycopg", "redis", "sqlalchemy"):
            violations.append(f"{fp}: forbidden import: {imp}")
print("COMPONENT violations:", [v for v in violations if "components" in v] or "NONE")
print("TOTAL violations:", violations or "NONE")
