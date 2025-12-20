# 📊 Portfolio Tracker

Dashboard web para gestionar tu cartera de inversiones.

## 🚀 Deploy en Render (Gratis)

### Paso 1: Crear cuenta en Render
1. Ve a [render.com](https://render.com)
2. Regístrate con GitHub

### Paso 2: Subir código a GitHub
1. Crea un repositorio en GitHub
2. Sube los archivos de portfolio_tracker:
```bash
git init
git add .
git commit -m "Portfolio Tracker v1"
git remote add origin https://github.com/TU_USUARIO/portfolio-tracker.git
git push -u origin main
```

### Paso 3: Crear Base de Datos en Render
1. En Render Dashboard → New → PostgreSQL
2. Nombre: `portfolio-db`
3. Plan: Free
4. Crear → Copiar "Internal Database URL"

### Paso 4: Crear Web Service en Render
1. New → Web Service
2. Conectar tu repo de GitHub
3. Configurar:
   - Name: `portfolio-tracker`
   - Runtime: Python 3
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn web_app:app --bind 0.0.0.0:$PORT`

### Paso 5: Variables de Entorno
En Render → Environment, añadir:

| Variable | Valor |
|----------|-------|
| `DATABASE_URL` | (la URL de PostgreSQL del paso 3) |
| `SECRET_KEY` | (genera una: `python -c "import secrets; print(secrets.token_hex(32))"`) |
| `REQUIRE_AUTH` | `true` |
| `ADMIN_USER` | tu_usuario |
| `ADMIN_PASS` | tu_contraseña_segura |
| `FLASK_ENV` | `production` |

### Paso 6: Deploy
1. Click en "Create Web Service"
2. Espera 2-3 minutos
3. ¡Listo! Tu app estará en `https://portfolio-tracker-xxxx.onrender.com`

---

## 💻 Desarrollo Local

```bash
# Instalar dependencias
pip install -r requirements.txt

# Ejecutar (modo desarrollo, sin auth)
python web_app.py --port 9000

# Ejecutar con autenticación
REQUIRE_AUTH=true ADMIN_USER=admin ADMIN_PASS=test123 python web_app.py
```

---

## 📱 Instalar como App (PWA)

1. Abre la web en Safari (iPhone) o Chrome (Android)
2. Menú Compartir → "Añadir a pantalla de inicio"
3. ¡Listo! Tendrás un icono como app nativa

---

## 🔧 Características

- ✅ Dashboard con resumen de cartera
- ✅ Gráficos de evolución y distribución
- ✅ Alertas de precio
- ✅ Target Allocation y rebalanceo automático
- ✅ Análisis técnico (RSI, Bollinger, soportes/resistencias)
- ✅ Explorar activos sin añadirlos
- ✅ Backup/Restore de datos
- ✅ PWA para móvil
- ✅ Autenticación básica

---

## 📄 Licencia

MIT - Uso libre para proyectos personales
