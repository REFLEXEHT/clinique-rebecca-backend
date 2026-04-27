# Déploiement Backend — Render

## Corrections appliquées

### Sécurité critique
- Mot de passe admin hardcodé (`rebecca2026`) remplacé par variable d'environnement `ADMIN_DEFAULT_PASSWORD`
- `allow_origins=["*"]` remplacé par liste explicite via `CORS_ORIGINS`
- `allow_credentials=False` (Bearer token = pas de cookies → pas besoin de credentials)
- `SECRET_KEY` utilise `secrets.token_urlsafe(32)` comme fallback (doit être défini en prod)
- Documentation Swagger désactivée en production (`ENVIRONMENT=production`)

### Bonnes pratiques
- Credentials admin jamais dans le code source
- Log informatif si `ADMIN_DEFAULT_PASSWORD` non défini

## Variables d'environnement Render (obligatoires)
| Variable | Valeur |
|----------|--------|
| `DATABASE_URL` | Fourni automatiquement par Render PostgreSQL |
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(64))"` |
| `ADMIN_DEFAULT_PASSWORD` | Mot de passe fort pour le compte admin |
| `ENVIRONMENT` | `production` |
| `CORS_ORIGINS` | `https://clinique-rebecca.vercel.app` |

## Variables optionnelles
| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Pour le chat IA |
| `SMTP_USER` / `SMTP_PASSWORD` | Pour les emails |

## Commande de démarrage Render
```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Vérification
- `GET /health` → `{"status": "healthy"}`
- `GET /docs` → Swagger (désactivé en production)
