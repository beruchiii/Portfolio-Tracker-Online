# 📊 Portfolio Tracker

Aplicación para el seguimiento de tu cartera de inversiones con **dos interfaces**:
- 🖥️ **Terminal** - Interfaz de línea de comandos visual
- 🌐 **Web** - Dashboard interactivo en el navegador

## ✨ Características

- ✅ Registro de posiciones por ISIN (sin necesitar ticker)
- ✅ **Sistema multi-fuente de cotizaciones:**
  - 1️⃣ Yahoo Finance (acciones, ETFs internacionales)
  - 2️⃣ justETF API (ETFs europeos) ⭐
  - 3️⃣ Morningstar (fondos europeos)
- ✅ **Dashboard web** con gráficos comparativos
- ✅ Cálculo automático de rentabilidad (€ y %)
- ✅ Comparativa visual entre posiciones
- ✅ Datos guardados localmente en JSON

## 🚀 Instalación

```bash
# 1. Descomprimir
unzip portfolio_tracker_web.zip
cd portfolio_tracker

# 2. Crear entorno virtual
python3 -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Instalar dependencias
pip install -r requirements.txt
```

## 📖 Uso

### 🌐 Versión Web (Recomendada)

```bash
python web_app.py
```

Abre tu navegador en: **http://localhost:5000**

![Dashboard](https://via.placeholder.com/800x400?text=Dashboard+Preview)

### 🖥️ Versión Terminal

```bash
python main.py
```

## 🌐 Características de la Versión Web

### Dashboard
- 📊 **Resumen de cartera** - Valor total, beneficio, rentabilidad
- 📈 **Gráfico de rentabilidad** - Compara el rendimiento de cada posición
- 🥧 **Distribución de cartera** - Ve el peso de cada activo
- 💹 **Beneficio por posición** - Gráfico de ganancias/pérdidas
- 📋 **Tabla de posiciones** - Detalle completo de cada activo

### Añadir Posición
- 🔍 **Búsqueda por ISIN** - Encuentra automáticamente el activo
- ✅ **Validación en tiempo real** - Verifica el activo antes de añadir
- 📝 **Formulario guiado** - Paso a paso para no olvidar nada

## 📁 Estructura del proyecto

```
portfolio_tracker/
├── data/
│   └── portfolio.json      # Tus datos
├── src/
│   ├── models.py           # Modelos de datos
│   ├── price_fetcher.py    # Sistema multi-fuente
│   ├── scrapers.py         # Scrapers y API justETF
│   └── reports.py          # Análisis y cálculos
├── templates/
│   ├── index.html          # Dashboard web
│   └── add_position.html   # Formulario añadir
├── static/                 # Archivos estáticos
├── main.py                 # App terminal
├── web_app.py              # App web (Flask)
└── requirements.txt
```

## 🛠️ Tecnologías

- **Python 3.8+**
- **Flask** - Servidor web
- **Tailwind CSS** - Estilos
- **Chart.js** - Gráficos
- **Rich** - Interfaz terminal
- **yfinance** - Yahoo Finance
- **BeautifulSoup4** - Scraping

## 📝 Notas

- Los datos se guardan en `data/portfolio.json`
- Los precios se actualizan cada vez que cargas el dashboard
- Compatible con Windows, macOS y Linux
