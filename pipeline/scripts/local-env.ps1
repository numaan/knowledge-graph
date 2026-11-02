# Local test settings for the kg-* commands (PowerShell).
#   . .\scripts\local-env.ps1            (note the leading dot: it sets variables in your session)
# Reads NEO4J_PASSWORD from an env file (a line NEO4J_PASSWORD=...); the password is not stored in this script.
#   . .\scripts\local-env.ps1 -PasswordFile C:\path\to\neo4j.env
# Default: $env:KG_ENV_FILE, else pipeline\.env.local (ignored by git).
param(
  [string]$PasswordFile = $(if ($env:KG_ENV_FILE) { $env:KG_ENV_FILE } else { "$PSScriptRoot\..\.env.local" })
)

$line = Get-Content $PasswordFile | Where-Object { $_ -like "NEO4J_PASSWORD=*" } | Select-Object -First 1
$env:NEO4J_PASSWORD = $line.Substring("NEO4J_PASSWORD=".Length)
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:EMBEDDINGS_URL = "http://localhost:11434"
$env:EMBEDDINGS_MODEL = "qwen3-embedding:0.6b"
$env:EMBEDDINGS_DIMENSIONS = "1024"
$env:LLM_PROVIDER = "ollama"
$env:LLM_BASE_URL = "http://localhost:11434"
$env:LLM_MODEL = "qwen3.6:35b-a3b"
$env:LLM_CACHE_DIR = "$PSScriptRoot\..\.kg-cache\llm"
$env:PYTHONUTF8 = "1"
Write-Host "kg-pipeline local settings loaded (Neo4j $env:NEO4J_URI, LLM $env:LLM_MODEL)"
