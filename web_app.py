#!/usr/bin/env python3
"""
Portfolio Tracker - Versión Web Multiusuario
Dashboard interactivo para seguimiento de cartera
"""
import os
import sys
import json
import uuid
import requests
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, jsonify, request, redirect, url_for, session, g

# Añadir src al path
sys.path.insert(0, str(Path(__file__).parent))

from src.models import Portfolio, Position
from src.reports import PortfolioAnalyzer
from src.price_fetcher import price_fetcher

# Configuración
app = Flask(__name__, template_folder='templates', static_folder='static')

# Configuración desde variables de entorno
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-portfolio-tracker')

# Base de datos (si está disponible)
DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_DATABASE = bool(DATABASE_URL)

# Credenciales admin por defecto (para crear usuario inicial)
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'portfolio2024')

if USE_DATABASE:
    # Render usa postgres://, SQLAlchemy necesita postgresql://
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
    
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    
    from database import db, Posicion, Aportacion, Alerta, Target, ActivoNuevo, TelegramConfig, Usuario, Favorito, migrar_datos_a_usuario
    db.init_app(app)
    
    with app.app_context():
        # Crear tablas
        db.create_all()
        
        # Añadir columnas nuevas si no existen (migración)
        try:
            from sqlalchemy import text
            with db.engine.connect() as conn:
                # Columnas user_id en tablas existentes
                for tabla in ['posiciones', 'alertas', 'targets', 'activos_nuevos', 'telegram_config']:
                    try:
                        conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES usuarios(id)"))
                    except:
                        pass
                # Columnas nuevas en usuarios
                try:
                    conn.execute(text("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS nombre VARCHAR(100)"))
                    conn.execute(text("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE"))
                    conn.execute(text("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE"))
                except:
                    pass
                # Columna notificada en alertas
                try:
                    conn.execute(text("ALTER TABLE alertas ADD COLUMN IF NOT EXISTS notificada BOOLEAN DEFAULT FALSE"))
                except:
                    pass
                conn.commit()
        except Exception as e:
            print(f"⚠️ Migración: {e}")
        
        # Crear usuario admin si no existe
        admin = Usuario.query.filter_by(username=ADMIN_USER).first()
        if not admin:
            admin = Usuario(
                username=ADMIN_USER,
                nombre='Administrador',
                is_admin=True,
                activo=True
            )
            admin.set_password(ADMIN_PASS)
            db.session.add(admin)
            db.session.commit()
            print(f"✅ Usuario admin '{ADMIN_USER}' creado")
            
            # Migrar datos existentes al admin
            migrar_datos_a_usuario(admin.id)
        else:
            # Asegurar que es admin
            if not admin.is_admin:
                admin.is_admin = True
                db.session.commit()
    
    print("✅ Usando base de datos PostgreSQL (Multiusuario)")
else:
    print("📁 Usando archivos JSON locales")

# CORS
from flask_cors import CORS
CORS(app)

# Autenticación siempre requerida en modo BD
REQUIRE_AUTH = USE_DATABASE or os.environ.get('REQUIRE_AUTH', 'false').lower() == 'true'

def get_current_user():
    """Obtiene el usuario actual de la sesión"""
    if not USE_DATABASE:
        return None
    user_id = session.get('user_id')
    if user_id:
        return Usuario.query.get(user_id)
    return None

def login_required(f):
    """Decorador para requerir autenticación"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if REQUIRE_AUTH and not session.get('logged_in'):
            if request.is_json:
                return jsonify({'success': False, 'error': 'No autorizado'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """Decorador para requerir ser administrador"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            if request.is_json:
                return jsonify({'success': False, 'error': 'No autorizado'}), 401
            return redirect(url_for('login'))
        
        if USE_DATABASE:
            user = get_current_user()
            if not user or not user.is_admin:
                if request.is_json:
                    return jsonify({'success': False, 'error': 'Se requiere ser administrador'}), 403
                return redirect('/')
        
        return f(*args, **kwargs)
    return decorated_function

DATA_DIR = Path(__file__).parent / "data"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"
CACHE_FILE = DATA_DIR / "price_cache.json"
ALERTS_FILE = DATA_DIR / "alerts.json"
FAVORITES_FILE = DATA_DIR / "favorites.json"

# Duración del caché en minutos
CACHE_DURATION_MINUTES = 15


# =============================================================================
# AUTENTICACIÓN
# =============================================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Página de login"""
    if not REQUIRE_AUTH:
        return redirect('/')
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        
        if USE_DATABASE:
            # Login con base de datos
            user = Usuario.query.filter_by(username=username, activo=True).first()
            if user and user.check_password(password):
                session['logged_in'] = True
                session['user_id'] = user.id
                session['username'] = user.username
                session['is_admin'] = user.is_admin
                session['nombre'] = user.nombre or user.username
                
                # Actualizar último acceso
                user.ultimo_acceso = datetime.utcnow()
                db.session.commit()
                
                return redirect('/')
        else:
            # Login con variables de entorno (modo local)
            if username == ADMIN_USER and password == ADMIN_PASS:
                session['logged_in'] = True
                session['username'] = username
                session['is_admin'] = True
                return redirect('/')
        
        return render_template('login.html', error='Usuario o contraseña incorrectos')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    """Cerrar sesión"""
    session.clear()
    return redirect('/login')


# =============================================================================
# SISTEMA DE ALERTAS
# =============================================================================

def cargar_alertas(user_id=None):
    """Carga las alertas desde archivo o BD"""
    if USE_DATABASE:
        # Si no se especifica user_id, usar el de la sesión
        if user_id is None:
            user_id = session.get('user_id')
        
        if user_id:
            alertas = Alerta.query.filter_by(user_id=user_id).all()
        else:
            alertas = Alerta.query.all()
        return [a.to_dict() for a in alertas]
    
    if ALERTS_FILE.exists():
        try:
            with open(ALERTS_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return []

def guardar_alertas(alertas, user_id=None):
    """Guarda las alertas en archivo o BD"""
    if USE_DATABASE:
        if user_id is None:
            user_id = session.get('user_id')
        
        # Solo eliminar alertas del usuario actual
        if user_id:
            Alerta.query.filter_by(user_id=user_id).delete()
        
        for al_data in alertas:
            alerta = Alerta(
                id=al_data.get('id'),
                user_id=user_id,
                isin=al_data.get('isin'),
                nombre=al_data.get('nombre'),
                tipo=al_data.get('tipo'),
                precio_objetivo=al_data.get('precio_objetivo'),
                precio_actual=al_data.get('precio_actual'),
                precio_referencia=al_data.get('precio_referencia'),
                objetivo_pct=al_data.get('objetivo_pct'),
                ticker=al_data.get('ticker'),
                activa=al_data.get('activa', True),
                disparada=al_data.get('disparada', False)
            )
            db.session.add(alerta)
        db.session.commit()
    else:
        DATA_DIR.mkdir(exist_ok=True)
        with open(ALERTS_FILE, 'w') as f:
            json.dump(alertas, f, indent=2)


# =============================================================================
# FAVORITOS / WATCHLIST
# =============================================================================

def cargar_favoritos(user_id=None):
    """Carga los favoritos desde archivo o BD"""
    if USE_DATABASE:
        if user_id is None:
            user_id = session.get('user_id')
        
        if user_id:
            favoritos = Favorito.query.filter_by(user_id=user_id).order_by(Favorito.fecha_agregado.desc()).all()
        else:
            favoritos = Favorito.query.order_by(Favorito.fecha_agregado.desc()).all()
        return [f.to_dict() for f in favoritos]
    
    if FAVORITES_FILE.exists():
        try:
            with open(FAVORITES_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return []

def guardar_favoritos(favoritos, user_id=None):
    """Guarda los favoritos en archivo o BD"""
    if USE_DATABASE:
        if user_id is None:
            user_id = session.get('user_id')
        
        # Solo eliminar favoritos del usuario actual
        if user_id:
            Favorito.query.filter_by(user_id=user_id).delete()
        
        for fav_data in favoritos:
            favorito = Favorito(
                id=fav_data.get('id'),
                user_id=user_id,
                ticker=fav_data.get('ticker'),
                isin=fav_data.get('isin'),
                nombre=fav_data.get('nombre'),
                notas=fav_data.get('notas', ''),
                fecha_agregado=fav_data.get('fecha_agregado')
            )
            db.session.add(favorito)
        db.session.commit()
    else:
        DATA_DIR.mkdir(exist_ok=True)
        with open(FAVORITES_FILE, 'w') as f:
            json.dump(favoritos, f, indent=2)

def agregar_favorito(ticker, isin=None, nombre=None, notas=''):
    """Agrega un activo a favoritos"""
    favoritos = cargar_favoritos()
    
    # Verificar si ya existe
    for fav in favoritos:
        if fav.get('ticker') == ticker or (isin and fav.get('isin') == isin):
            return False  # Ya existe
    
    nuevo_fav = {
        'id': str(uuid.uuid4()),
        'ticker': ticker,
        'isin': isin,
        'nombre': nombre or ticker,
        'notas': notas,
        'fecha_agregado': datetime.now().isoformat()
    }
    
    favoritos.append(nuevo_fav)
    guardar_favoritos(favoritos)
    return True

def eliminar_favorito(favorito_id=None, ticker=None):
    """Elimina un favorito por ID o ticker"""
    favoritos = cargar_favoritos()
    
    favoritos_filtrados = []
    eliminado = False
    
    for fav in favoritos:
        if favorito_id and fav.get('id') == favorito_id:
            eliminado = True
            continue
        if ticker and (fav.get('ticker') == ticker or fav.get('isin') == ticker):
            eliminado = True
            continue
        favoritos_filtrados.append(fav)
    
    if eliminado:
        guardar_favoritos(favoritos_filtrados)
    
    return eliminado

def es_favorito(ticker=None, isin=None):
    """Verifica si un activo está en favoritos"""
    favoritos = cargar_favoritos()
    
    for fav in favoritos:
        if ticker and fav.get('ticker') == ticker:
            return True
        if isin and fav.get('isin') == isin:
            return True
    
    return False


def verificar_alertas():
    """Verifica si alguna alerta se ha disparado"""
    alertas = cargar_alertas()
    alertas_disparadas = []
    
    for alerta in alertas:
        if alerta.get('estado') == 'disparada' or not alerta.get('activa', True):
            continue
            
        try:
            # Obtener precio actual
            ticker = alerta.get('ticker') or alerta.get('isin')
            resultado_precio = price_fetcher.obtener_precio(ticker, alerta.get('isin'))
            
            if not resultado_precio or not resultado_precio.get('precio'):
                continue
            
            precio_actual = float(resultado_precio['precio'])
            alerta['precio_actual'] = precio_actual
            precio_referencia = alerta.get('precio_referencia', 0)
            
            if precio_referencia <= 0:
                continue
            
            disparada = False
            tipo = alerta.get('tipo', 'baja')
            objetivo_pct = alerta.get('objetivo_pct', 0)
            
            # Calcular cambio porcentual
            cambio_pct = ((precio_actual - precio_referencia) / precio_referencia) * 100
            
            if tipo == 'baja':
                # Alerta cuando baja X% desde precio referencia
                if cambio_pct <= -abs(objetivo_pct):
                    disparada = True
            elif tipo == 'sube':
                # Alerta cuando sube X% desde precio referencia
                if cambio_pct >= abs(objetivo_pct):
                    disparada = True
            
            if disparada:
                alerta['estado'] = 'disparada'
                alerta['fecha_disparada'] = datetime.now().isoformat()
                alerta['cambio_pct'] = cambio_pct
                alertas_disparadas.append(alerta)
                
        except Exception as e:
            print(f"Error verificando alerta: {e}")
            continue
    
    # Guardar estado actualizado
    guardar_alertas(alertas)
    
    return alertas_disparadas


# =============================================================================
# SISTEMA DE CACHÉ DE PRECIOS
# =============================================================================

def cargar_cache():
    """Carga el caché de precios desde archivo"""
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'prices': {}, 'last_update': None}

def guardar_cache(cache):
    """Guarda el caché de precios en archivo"""
    DATA_DIR.mkdir(exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

def obtener_precio_con_cache(isin, ticker=None, force_refresh=False):
    """Obtiene precio usando caché si está disponible y no ha expirado"""
    cache = cargar_cache()
    ahora = datetime.now()
    
    # Verificar si hay precio en caché y no ha expirado
    if not force_refresh and isin in cache['prices']:
        cached = cache['prices'][isin]
        cached_time = datetime.fromisoformat(cached['timestamp'])
        
        if (ahora - cached_time) < timedelta(minutes=CACHE_DURATION_MINUTES):
            return cached['data']
    
    # Obtener precio fresco
    if ticker:
        precio_data = price_fetcher.obtener_precio(ticker, isin)
    else:
        precio_data = price_fetcher.obtener_precio_por_isin(isin)
    
    if precio_data:
        # Guardar en caché
        cache['prices'][isin] = {
            'data': precio_data,
            'timestamp': ahora.isoformat()
        }
        cache['last_update'] = ahora.isoformat()
        guardar_cache(cache)
    
    return precio_data

def invalidar_cache():
    """Invalida todo el caché de precios"""
    cache = {'prices': {}, 'last_update': None}
    guardar_cache(cache)
    return cache


def cargar_portfolio(user_id=None) -> Portfolio:
    """Carga el portfolio desde archivo o BD"""
    if USE_DATABASE:
        # Si no se especifica user_id, usar el de la sesión
        if user_id is None:
            user_id = session.get('user_id')
        
        if user_id:
            posiciones = Posicion.query.filter_by(user_id=user_id).all()
        else:
            posiciones = Posicion.query.all()
        
        portfolio = Portfolio()
        for pos_db in posiciones:
            pos = Position(
                isin=pos_db.isin,
                nombre=pos_db.nombre,
                ticker=pos_db.ticker,
                categoria=pos_db.categoria
            )
            pos.id = pos_db.id
            for ap_db in pos_db.aportaciones:
                # Crear objeto Aportacion en lugar de dict
                from src.models import Aportacion as AportacionModel
                ap = AportacionModel(
                    cantidad=ap_db.cantidad,
                    precio_compra=ap_db.precio,
                    fecha_compra=ap_db.fecha.isoformat() if ap_db.fecha else '',
                    broker=getattr(ap_db, 'broker', '') or '',
                    notas=ap_db.notas or ''
                )
                pos.aportaciones.append(ap)
            portfolio.posiciones.append(pos)
        return portfolio
    else:
        DATA_DIR.mkdir(exist_ok=True)
        if PORTFOLIO_FILE.exists():
            return Portfolio.cargar(str(PORTFOLIO_FILE))
        return Portfolio()


def guardar_portfolio(portfolio: Portfolio, user_id=None):
    """Guarda el portfolio en archivo o BD"""
    if USE_DATABASE:
        try:
            # Si no se especifica user_id, usar el de la sesión
            if user_id is None:
                user_id = session.get('user_id')
            
            # Solo eliminar posiciones del usuario actual
            if user_id:
                posiciones_usuario = Posicion.query.filter_by(user_id=user_id).all()
                for pos in posiciones_usuario:
                    Aportacion.query.filter_by(posicion_id=pos.id).delete()
                Posicion.query.filter_by(user_id=user_id).delete()
            
            for pos in portfolio.posiciones:
                pos_db = Posicion(
                    id=pos.id or pos.isin,
                    user_id=user_id,
                    isin=pos.isin,
                    ticker=pos.ticker,
                    nombre=pos.nombre,
                    categoria=pos.categoria
                )
                db.session.add(pos_db)
                
                for ap in pos.aportaciones:
                    # Soportar tanto formato dict como objeto Aportacion
                    if hasattr(ap, 'fecha_compra'):
                        fecha = ap.fecha_compra
                        precio = ap.precio_compra
                        cantidad = ap.cantidad
                        comision = getattr(ap, 'comision', 0)
                    else:
                        fecha = ap.get('fecha_compra') or ap.get('fecha')
                        precio = ap.get('precio_compra') or ap.get('precio')
                        cantidad = ap.get('cantidad', 0)
                        comision = ap.get('comision', 0)
                    
                    if isinstance(fecha, str):
                        try:
                            fecha = datetime.fromisoformat(fecha).date()
                        except:
                            fecha = datetime.strptime(fecha, '%Y-%m-%d').date()
                    elif fecha is None:
                        fecha = datetime.utcnow().date()
                    
                    ap_db = Aportacion(
                        posicion_id=pos_db.id,
                        fecha=fecha,
                        cantidad=cantidad,
                        precio=precio,
                        comision=comision or 0
                    )
                    db.session.add(ap_db)
            
            db.session.commit()
            print(f"✅ Portfolio guardado: {len(portfolio.posiciones)} posiciones")
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error guardando portfolio: {e}")
            raise
    else:
        DATA_DIR.mkdir(exist_ok=True)
        portfolio.guardar(str(PORTFOLIO_FILE))


# =============================================================================
# RUTAS WEB (HTML)
# =============================================================================

@app.route('/')
@login_required
def index():
    """Página principal - Dashboard"""
    return render_template('index.html')


@app.route('/add')
@login_required
def add_position_page():
    """Página para añadir posición"""
    return render_template('add_position.html')


@app.route('/explorar')
@login_required
def explorar_page():
    """Página para explorar y analizar activos sin añadirlos a la cartera"""
    return render_template('explorar.html')


@app.route('/alertas')
@login_required
def alertas_page():
    """Página para gestionar alertas de precio"""
    return render_template('alertas.html')


# =============================================================================
# API DE ALERTAS
# =============================================================================

@app.route('/api/alertas')
@login_required
def api_get_alertas():
    """Obtiene todas las alertas"""
    alertas = cargar_alertas()
    
    # Actualizar precios actuales
    for alerta in alertas:
        try:
            resultado_precio = price_fetcher.obtener_precio(alerta.get('ticker'), alerta.get('isin'))
            if resultado_precio and resultado_precio.get('precio'):
                precio_actual = float(resultado_precio['precio'])
                alerta['precio_actual'] = precio_actual
                
                # Calcular % de cambio desde precio de referencia
                precio_ref = alerta.get('precio_referencia', 0)
                if precio_ref > 0:
                    cambio_pct = ((precio_actual - precio_ref) / precio_ref) * 100
                    alerta['cambio_pct'] = cambio_pct
                    
                    # Verificar si se cumple la condición
                    tipo = alerta.get('tipo', 'baja')
                    objetivo_pct = alerta.get('objetivo_pct', 0)
                    
                    if tipo == 'baja':
                        alerta['cumplida'] = cambio_pct <= -abs(objetivo_pct)
                    else:  # sube
                        alerta['cumplida'] = cambio_pct >= abs(objetivo_pct)
                else:
                    alerta['cambio_pct'] = 0
                    alerta['cumplida'] = False
            else:
                alerta['precio_actual'] = None
                alerta['cumplida'] = False
        except Exception as e:
            alerta['precio_actual'] = None
            alerta['cumplida'] = False
    
    return jsonify({'success': True, 'data': alertas})


@app.route('/api/alertas', methods=['POST'])
def api_crear_alerta():
    """Crea una nueva alerta"""
    try:
        data = request.json
        
        # Validar datos requeridos
        if not data.get('isin') and not data.get('ticker'):
            return jsonify({'success': False, 'error': 'ISIN o Ticker requerido'})
        
        # Obtener precio actual como referencia
        isin = data.get('isin', '')
        ticker = data.get('ticker', '')
        resultado_precio = price_fetcher.obtener_precio(ticker, isin)
        
        if not resultado_precio or not resultado_precio.get('precio'):
            return jsonify({'success': False, 'error': 'No se pudo obtener el precio actual'})
        
        # Obtener nombre - priorizar el del resultado, luego el proporcionado, luego ISIN/ticker
        nombre = data.get('nombre', '')
        if not nombre or nombre == isin or nombre == ticker:
            # Intentar obtener nombre de la respuesta de precio
            nombre = resultado_precio.get('nombre', '')
        if not nombre or nombre == isin or nombre == ticker:
            # Buscar en el portfolio si existe
            portfolio = cargar_portfolio()
            pos = portfolio.buscar_por_isin(isin) if isin else None
            if pos:
                nombre = pos.nombre
        if not nombre:
            nombre = ticker or isin
        
        precio_actual = float(resultado_precio['precio'])
        objetivo_pct = float(data.get('objetivo_pct', 5))
        tipo = data.get('tipo', 'baja')
        
        # Calcular precio objetivo
        if tipo == 'baja':
            precio_objetivo = precio_actual * (1 - objetivo_pct / 100)
        else:
            precio_objetivo = precio_actual * (1 + objetivo_pct / 100)
        
        # Crear alerta
        import uuid
        alerta = {
            'id': str(uuid.uuid4())[:8],
            'nombre': nombre,
            'isin': isin,
            'ticker': ticker,
            'tipo': tipo,
            'objetivo_pct': objetivo_pct,
            'precio_referencia': precio_actual,
            'precio_objetivo': precio_objetivo,
            'fecha_creacion': datetime.now().isoformat(),
            'activa': True,
            'notificada': False
        }
        
        alertas = cargar_alertas()
        alertas.append(alerta)
        guardar_alertas(alertas)
        
        return jsonify({'success': True, 'data': alerta})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/alertas/masiva', methods=['POST'])
def api_crear_alertas_masivas():
    """Crea alertas para múltiples activos o todos los de la cartera"""
    try:
        data = request.json
        tipo = data.get('tipo', 'baja')
        objetivo_pct = float(data.get('objetivo_pct', 5))
        isins = data.get('isins', [])  # Lista de ISINs específicos, o vacío para todos
        
        portfolio = cargar_portfolio()
        
        if not portfolio.posiciones:
            return jsonify({'success': False, 'error': 'No hay posiciones en la cartera'})
        
        # Si no se especifican ISINs, usar todos los de la cartera
        if not isins:
            posiciones_a_alertar = portfolio.posiciones
        else:
            posiciones_a_alertar = [p for p in portfolio.posiciones if p.isin in isins]
        
        if not posiciones_a_alertar:
            return jsonify({'success': False, 'error': 'No se encontraron posiciones para alertar'})
        
        alertas_creadas = []
        alertas_existentes = cargar_alertas()
        errores = []
        
        import uuid
        
        for pos in posiciones_a_alertar:
            try:
                # Verificar si ya existe alerta similar
                ya_existe = any(
                    a.get('isin') == pos.isin and 
                    a.get('tipo') == tipo and 
                    abs(a.get('objetivo_pct', 0) - objetivo_pct) < 0.1
                    for a in alertas_existentes
                )
                
                if ya_existe:
                    errores.append(f"{pos.nombre}: ya existe alerta similar")
                    continue
                
                # Obtener precio actual
                resultado_precio = price_fetcher.obtener_precio(pos.ticker, pos.isin)
                
                if not resultado_precio or not resultado_precio.get('precio'):
                    errores.append(f"{pos.nombre}: no se pudo obtener precio")
                    continue
                
                precio_actual = float(resultado_precio['precio'])
                
                # Calcular precio objetivo
                if tipo == 'baja':
                    precio_objetivo = precio_actual * (1 - objetivo_pct / 100)
                else:
                    precio_objetivo = precio_actual * (1 + objetivo_pct / 100)
                
                # Crear alerta
                alerta = {
                    'id': str(uuid.uuid4())[:8],
                    'nombre': pos.nombre,
                    'isin': pos.isin,
                    'ticker': pos.ticker,
                    'tipo': tipo,
                    'objetivo_pct': objetivo_pct,
                    'precio_referencia': precio_actual,
                    'precio_objetivo': precio_objetivo,
                    'fecha_creacion': datetime.now().isoformat(),
                    'activa': True,
                    'notificada': False
                }
                
                alertas_existentes.append(alerta)
                alertas_creadas.append(alerta)
                
            except Exception as e:
                errores.append(f"{pos.nombre}: {str(e)}")
        
        # Guardar todas las alertas
        if alertas_creadas:
            guardar_alertas(alertas_existentes)
        
        return jsonify({
            'success': True,
            'data': {
                'creadas': len(alertas_creadas),
                'alertas': alertas_creadas,
                'errores': errores
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/alertas/<alerta_id>', methods=['DELETE'])
def api_eliminar_alerta(alerta_id):
    """Elimina una alerta"""
    try:
        alertas = cargar_alertas()
        alertas = [a for a in alertas if a.get('id') != alerta_id]
        guardar_alertas(alertas)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/alertas/<alerta_id>/toggle', methods=['POST'])
def api_toggle_alerta(alerta_id):
    """Activa/desactiva una alerta"""
    try:
        alertas = cargar_alertas()
        for alerta in alertas:
            if alerta.get('id') == alerta_id:
                alerta['activa'] = not alerta.get('activa', True)
                break
        guardar_alertas(alertas)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/alertas/check')
def api_check_alertas():
    """Verifica qué alertas se han cumplido"""
    alertas = cargar_alertas()
    cumplidas = []
    
    for alerta in alertas:
        if not alerta.get('activa', True):
            continue
            
        try:
            resultado_precio = price_fetcher.obtener_precio(alerta.get('ticker'), alerta.get('isin'))
            if resultado_precio and resultado_precio.get('precio'):
                precio_actual = float(resultado_precio['precio'])
                precio_ref = alerta.get('precio_referencia', 0)
                if precio_ref > 0:
                    cambio_pct = ((precio_actual - precio_ref) / precio_ref) * 100
                    tipo = alerta.get('tipo', 'baja')
                    objetivo_pct = alerta.get('objetivo_pct', 0)
                    
                    cumplida = False
                    if tipo == 'baja' and cambio_pct <= -abs(objetivo_pct):
                        cumplida = True
                    elif tipo == 'sube' and cambio_pct >= abs(objetivo_pct):
                        cumplida = True
                    
                    if cumplida and not alerta.get('notificada', False):
                        cumplidas.append({
                            **alerta,
                            'precio_actual': precio_actual,
                            'cambio_pct': cambio_pct
                        })
        except:
            pass
    
    return jsonify({'success': True, 'data': cumplidas})


# =============================================================================
# FAVORITOS / WATCHLIST
# =============================================================================

@app.route('/api/favoritos')
@login_required
def api_favoritos():
    """Obtiene la lista de favoritos con precios actualizados"""
    favoritos = cargar_favoritos()
    favoritos_actualizados = False
    
    # Actualizar precios de cada favorito
    for fav in favoritos:
        try:
            ticker = fav.get('ticker') or fav.get('isin')
            isin = fav.get('isin')
            resultado = price_fetcher.obtener_precio(ticker, isin)
            
            if resultado and resultado.get('precio'):
                precio_actual = resultado['precio']
                fav['precio_actual'] = precio_actual
                
                # Si no tiene precio_al_agregar, guardarlo ahora
                if not fav.get('precio_al_agregar'):
                    fav['precio_al_agregar'] = precio_actual
                    favoritos_actualizados = True
                
                # Calcular cambio desde que se agregó
                precio_agregado = fav.get('precio_al_agregar')
                if precio_agregado and precio_agregado > 0:
                    fav['cambio_desde_agregado'] = ((precio_actual - precio_agregado) / precio_agregado) * 100
                else:
                    fav['cambio_desde_agregado'] = None
                    
                # Cambio diario - usar JustETF para ETFs europeos (más preciso)
                cambio_diario = None
                mensaje_cierre = None
                try:
                    es_etf_europeo = isin and isin[:2] in ['IE', 'LU', 'DE', 'FR', 'NL', 'GB']
                    
                    # 1. Para ETFs europeos: comparar JustETF y Yahoo, usar el más reciente
                    if es_etf_europeo and isin:
                        from src.scrapers import obtener_cambio_diario_con_info
                        info_cambio = obtener_cambio_diario_con_info(isin, ticker)
                        if info_cambio:
                            cambio_diario = info_cambio.get('cambio')
                            mensaje_cierre = info_cambio.get('mensaje')
                            fuente = info_cambio.get('fuente', '')
                            if cambio_diario is not None:
                                print(f"[Favoritos] {fuente.upper()} para {isin}: {cambio_diario:.2f}% - {mensaje_cierre}")
                    
                    # 2. Si no es ETF europeo o JustETF falló, usar Yahoo Finance
                    if cambio_diario is None and ticker and not ticker.startswith('IE'):
                        import yfinance as yf
                        stock = yf.Ticker(ticker)
                        # Usar fast_info que tiene previousClose más fiable
                        info = stock.fast_info
                        last_price = getattr(info, 'last_price', None)
                        prev_close = getattr(info, 'previous_close', None)
                        
                        if last_price and prev_close and prev_close > 0:
                            cambio_diario = ((last_price - prev_close) / prev_close) * 100
                            # Construir mensaje para Yahoo
                            from datetime import datetime
                            ahora = datetime.now()
                            es_fin_de_semana = ahora.weekday() >= 5
                            hora_actual = ahora.hour
                            # NYSE/NASDAQ: 9:30-16:00 EST (15:30-22:00 CET)
                            fuera_horario = hora_actual < 15 or hora_actual >= 22
                            mercado_cerrado = es_fin_de_semana or fuera_horario
                            if mercado_cerrado:
                                mensaje_cierre = "Mercado cerrado"
                            print(f"[Favoritos] Yahoo fast_info para {ticker}: {cambio_diario:.2f}%")
                        else:
                            # Fallback al método history
                            hist = stock.history(period='5d')
                            if len(hist) >= 2:
                                precio_ayer = float(hist['Close'].iloc[-2])
                                precio_hoy = float(hist['Close'].iloc[-1])
                                cambio_diario = ((precio_hoy - precio_ayer) / precio_ayer) * 100
                                # Obtener fecha del último dato
                                fecha_ultimo = hist.index[-1].strftime('%d %b %Y')
                                mensaje_cierre = f"Al cierre: {fecha_ultimo}"
                    
                    # 3. Fallback final: JustETF histórico si tenemos ISIN
                    if cambio_diario is None and isin:
                        from src.scrapers import JustETFScraper
                        scraper = JustETFScraper()
                        historico = scraper.obtener_historico(isin, periodo='1m')
                        if historico and len(historico.get('precios', [])) >= 2:
                            precios = historico['precios']
                            fechas = historico.get('fechas', [])
                            precio_ayer = precios[-2]
                            precio_hoy = precios[-1]
                            cambio_diario = ((precio_hoy - precio_ayer) / precio_ayer) * 100
                            if fechas:
                                mensaje_cierre = f"Al cierre: {fechas[-1]}"
                except Exception as e:
                    print(f"Error calculando cambio diario para {fav.get('nombre')}: {e}")
                
                fav['cambio_diario'] = cambio_diario
                fav['mensaje_cierre'] = mensaje_cierre
                
        except Exception as e:
            print(f"Error obteniendo precio de favorito {fav.get('nombre')}: {e}")
            fav['precio_actual'] = None
            fav['cambio_desde_agregado'] = None
            fav['cambio_diario'] = None
    
    # Guardar si se actualizaron precios_al_agregar
    if favoritos_actualizados:
        guardar_favoritos(favoritos)
    
    return jsonify({'success': True, 'data': favoritos})


@app.route('/api/favoritos', methods=['POST'])
@login_required
def api_agregar_favorito():
    """Agrega un activo a favoritos"""
    data = request.json
    
    ticker = data.get('ticker')
    isin = data.get('isin')
    nombre = data.get('nombre')
    notas = data.get('notas', '')
    
    if not ticker and not isin:
        return jsonify({'success': False, 'error': 'Se requiere ticker o ISIN'})
    
    # Verificar si ya existe
    if es_favorito(ticker, isin):
        return jsonify({'success': False, 'error': 'Ya está en favoritos'})
    
    # Obtener precio actual para guardar
    precio_actual = None
    try:
        resultado = price_fetcher.obtener_precio(ticker or isin, isin)
        if resultado and resultado.get('precio'):
            precio_actual = resultado['precio']
            if not nombre:
                nombre = resultado.get('nombre', ticker or isin)
    except:
        pass
    
    favoritos = cargar_favoritos()
    
    nuevo_fav = {
        'id': str(uuid.uuid4()),
        'ticker': ticker,
        'isin': isin,
        'nombre': nombre or ticker or isin,
        'notas': notas,
        'fecha_agregado': datetime.now().isoformat(),
        'precio_al_agregar': precio_actual
    }
    
    favoritos.append(nuevo_fav)
    guardar_favoritos(favoritos)
    
    return jsonify({'success': True, 'data': nuevo_fav})


@app.route('/api/favoritos/<favorito_id>', methods=['DELETE'])
@login_required
def api_eliminar_favorito(favorito_id):
    """Elimina un favorito por ID"""
    if eliminar_favorito(favorito_id=favorito_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Favorito no encontrado'})


@app.route('/api/favoritos/check', methods=['POST'])
@login_required
def api_check_favorito():
    """Verifica si un activo está en favoritos"""
    data = request.json
    ticker = data.get('ticker')
    isin = data.get('isin')
    
    return jsonify({
        'success': True,
        'es_favorito': es_favorito(ticker, isin)
    })


@app.route('/api/favoritos/<favorito_id>/notas', methods=['PUT'])
@login_required
def api_actualizar_notas_favorito(favorito_id):
    """Actualiza las notas de un favorito"""
    data = request.json
    notas = data.get('notas', '')
    
    favoritos = cargar_favoritos()
    
    for fav in favoritos:
        if fav.get('id') == favorito_id:
            fav['notas'] = notas
            guardar_favoritos(favoritos)
            return jsonify({'success': True})
    
    return jsonify({'success': False, 'error': 'Favorito no encontrado'})


# =============================================================================
# API REST (JSON)
# =============================================================================

@app.route('/api/portfolio')
def api_portfolio():
    """Obtiene el resumen completo de la cartera"""
    force_refresh = request.args.get('refresh', 'false') == 'true'
    
    if force_refresh:
        invalidar_cache()
    
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({
            'success': True,
            'data': {
                'resumen': {
                    'total_invertido': 0,
                    'valor_actual': 0,
                    'beneficio_total': 0,
                    'rentabilidad_pct': 0,
                    'num_posiciones': 0,
                    'posiciones_ganadoras': 0,
                    'posiciones_perdedoras': 0
                },
                'posiciones': [],
                'last_update': None
            }
        })
    
    analyzer = PortfolioAnalyzer(portfolio)
    posiciones = analyzer.actualizar_precios()
    resumen = analyzer.resumen_cartera()
    
    # Obtener última actualización del caché
    cache = cargar_cache()
    last_update = cache.get('last_update')
    
    # Convertir posiciones a dict
    posiciones_data = []
    for pos in posiciones:
        # Convertir aportaciones
        aportaciones_data = []
        for ap in pos.aportaciones:
            aportaciones_data.append({
                'id': ap.id,
                'cantidad': ap.cantidad,
                'precio_compra': ap.precio_compra,
                'fecha_compra': ap.fecha_compra,
                'broker': ap.broker,
                'coste_total': ap.coste_total
            })
        
        posiciones_data.append({
            'id': pos.id,
            'isin': pos.isin,
            'ticker': pos.ticker,
            'nombre': pos.nombre,
            'cantidad': pos.cantidad,
            'precio_compra': pos.precio_medio,  # precio medio
            'precio_medio': pos.precio_medio,
            'precio_actual': pos.precio_actual,
            'coste_total': pos.coste_total,
            'valor_actual': pos.valor_actual,
            'beneficio': pos.beneficio,
            'rentabilidad_pct': pos.rentabilidad_pct,
            'fecha_compra': pos.fecha_primera_compra,
            'fecha_primera_compra': pos.fecha_primera_compra,
            'fecha_ultima_compra': pos.fecha_ultima_compra,
            'broker': pos.broker,
            'moneda': pos.moneda,
            'num_aportaciones': pos.num_aportaciones,
            'categoria': pos.categoria,
            'sector': pos.sector,
            'aportaciones': aportaciones_data
        })
    
    return jsonify({
        'success': True,
        'data': {
            'resumen': resumen,
            'posiciones': posiciones_data,
            'last_update': last_update
        }
    })


@app.route('/api/cache/status')
def api_cache_status():
    """Obtiene el estado del caché de precios"""
    cache = cargar_cache()
    
    ahora = datetime.now()
    last_update = cache.get('last_update')
    
    if last_update:
        last_update_dt = datetime.fromisoformat(last_update)
        age_minutes = (ahora - last_update_dt).total_seconds() / 60
        expired = age_minutes > CACHE_DURATION_MINUTES
    else:
        age_minutes = None
        expired = True
    
    return jsonify({
        'success': True,
        'data': {
            'last_update': last_update,
            'age_minutes': round(age_minutes, 1) if age_minutes else None,
            'expired': expired,
            'cache_duration': CACHE_DURATION_MINUTES,
            'num_cached': len(cache.get('prices', {}))
        }
    })


@app.route('/api/cache/refresh', methods=['POST'])
def api_refresh_cache():
    """Fuerza actualización de todos los precios"""
    try:
        invalidar_cache()
        
        portfolio = cargar_portfolio()
        if portfolio.posiciones:
            analyzer = PortfolioAnalyzer(portfolio)
            analyzer.actualizar_precios()
        
        cache = cargar_cache()
        
        return jsonify({
            'success': True,
            'message': 'Precios actualizados',
            'last_update': cache.get('last_update')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/comparison')
def api_comparison():
    """Obtiene datos para comparar posiciones"""
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': True, 'data': []})
    
    analyzer = PortfolioAnalyzer(portfolio)
    posiciones = analyzer.actualizar_precios()
    
    # Datos para gráficos comparativos
    comparison_data = {
        'nombres': [],
        'rentabilidades': [],
        'valores': [],
        'pesos': [],
        'beneficios': [],
        'colores': []
    }
    
    valor_total = sum(p.valor_actual for p in posiciones)
    
    for pos in posiciones:
        comparison_data['nombres'].append(pos.nombre[:30])  # Truncar nombre
        comparison_data['rentabilidades'].append(round(pos.rentabilidad_pct, 2))
        comparison_data['valores'].append(round(pos.valor_actual, 2))
        comparison_data['beneficios'].append(round(pos.beneficio, 2))
        
        peso = (pos.valor_actual / valor_total * 100) if valor_total > 0 else 0
        comparison_data['pesos'].append(round(peso, 2))
        
        # Color según rentabilidad
        if pos.rentabilidad_pct >= 0:
            comparison_data['colores'].append('#10b981')  # Verde
        else:
            comparison_data['colores'].append('#ef4444')  # Rojo
    
    return jsonify({
        'success': True,
        'data': comparison_data
    })


@app.route('/api/position/search', methods=['POST'])
def api_search_position():
    """Busca información de un activo por ISIN"""
    data = request.get_json()
    isin = data.get('isin', '').upper().strip()
    ticker = data.get('ticker', '').upper().strip()
    
    if not isin:
        return jsonify({'success': False, 'error': 'ISIN requerido'})
    
    # Buscar información
    if ticker:
        precio_data = price_fetcher.obtener_precio(ticker, isin)
    else:
        precio_data = price_fetcher.obtener_precio_por_isin(isin)
    
    if precio_data:
        return jsonify({
            'success': True,
            'data': {
                'nombre': precio_data.get('nombre', isin),
                'precio': precio_data.get('precio', 0),
                'moneda': precio_data.get('moneda', 'EUR'),
                'fuente': precio_data.get('fuente', 'N/A'),
                'tipo': precio_data.get('tipo', 'N/A')
            }
        })
    else:
        return jsonify({
            'success': False,
            'error': 'No se encontró el activo'
        })


@app.route('/api/position/add', methods=['POST'])
def api_add_position():
    """Añade una nueva posición o aportación"""
    data = request.get_json()
    
    # Validar campos requeridos
    required = ['isin', 'nombre', 'cantidad', 'precio_compra', 'fecha_compra']
    for field in required:
        if not data.get(field):
            return jsonify({'success': False, 'error': f'Campo {field} requerido'})
    
    # Validar que la fecha no sea futura
    try:
        fecha_compra = datetime.strptime(data['fecha_compra'], '%Y-%m-%d')
        if fecha_compra > datetime.now():
            return jsonify({'success': False, 'error': 'La fecha de compra no puede ser futura'})
    except ValueError:
        return jsonify({'success': False, 'error': 'Formato de fecha inválido (usar YYYY-MM-DD)'})
    
    # Validar cantidad y precio positivos
    try:
        cantidad = float(data['cantidad'])
        precio = float(data['precio_compra'])
        if cantidad <= 0:
            return jsonify({'success': False, 'error': 'La cantidad debe ser mayor que 0'})
        if precio <= 0:
            return jsonify({'success': False, 'error': 'El precio debe ser mayor que 0'})
    except ValueError:
        return jsonify({'success': False, 'error': 'Cantidad o precio inválidos'})
    
    try:
        portfolio = cargar_portfolio()
        isin = data['isin'].upper().strip()
        ticker = data.get('ticker', '').upper().strip()
        nombre = data['nombre']
        
        # Buscar si ya existe una posición con este ISIN
        existente = portfolio.buscar_por_isin(isin)
        
        if existente:
            # Añadir aportación a posición existente
            existente.agregar_aportacion(
                cantidad=cantidad,
                precio_compra=precio,
                fecha_compra=data['fecha_compra'],
                broker=data.get('broker', ''),
                notas=data.get('notas', '')
            )
            # Actualizar ticker si se proporcionó y no estaba
            if not existente.ticker and ticker:
                existente.ticker = ticker
            
            # Detectar categoría si no tiene
            if not existente.categoria:
                existente.categoria = detectar_categoria(existente.ticker or ticker, nombre, isin)
                
            guardar_portfolio(portfolio)
            return jsonify({
                'success': True, 
                'message': f'Aportación añadida a {existente.nombre}',
                'is_new': False,
                'num_aportaciones': existente.num_aportaciones,
                'categoria': existente.categoria
            })
        else:
            # Detectar categoría automáticamente
            categoria = detectar_categoria(ticker, nombre, isin)
            
            # Crear nueva posición
            posicion = Position(
                isin=isin,
                ticker=ticker,
                nombre=nombre,
                categoria=categoria
            )
            posicion.agregar_aportacion(
                cantidad=cantidad,
                precio_compra=precio,
                fecha_compra=data['fecha_compra'],
                broker=data.get('broker', ''),
                notas=data.get('notas', '')
            )
            
            portfolio.agregar_posicion(posicion)
            guardar_portfolio(portfolio)
            
            return jsonify({
                'success': True, 
                'message': 'Posición añadida correctamente',
                'is_new': True,
                'num_aportaciones': 1,
                'categoria': categoria,
                'categoria_auto': bool(categoria)
            })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/position/check/<isin>')
def api_check_position(isin):
    """Verifica si ya existe una posición con este ISIN"""
    portfolio = cargar_portfolio()
    existente = portfolio.buscar_por_isin(isin.upper())
    
    if existente:
        return jsonify({
            'success': True,
            'exists': True,
            'data': {
                'id': existente.id,
                'isin': existente.isin,
                'ticker': existente.ticker,
                'nombre': existente.nombre,
                'cantidad': existente.cantidad,
                'precio_medio': existente.precio_medio,
                'num_aportaciones': existente.num_aportaciones,
                'coste_total': existente.coste_total
            }
        })
    else:
        return jsonify({
            'success': True,
            'exists': False
        })


@app.route('/api/aportacion/delete', methods=['POST'])
def api_delete_aportacion():
    """Elimina una aportación específica"""
    data = request.get_json()
    isin = data.get('isin', '').upper()
    aportacion_id = data.get('aportacion_id', '')
    
    if not isin or not aportacion_id:
        return jsonify({'success': False, 'error': 'ISIN y aportacion_id requeridos'})
    
    try:
        portfolio = cargar_portfolio()
        result = portfolio.eliminar_aportacion(isin, aportacion_id)
        
        if result:
            guardar_portfolio(portfolio)
            return jsonify({'success': True, 'message': 'Aportación eliminada'})
        else:
            return jsonify({'success': False, 'error': 'Aportación no encontrada'})
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/export')
def api_export():
    """Exporta la cartera, alertas y targets como JSON descargable"""
    try:
        portfolio = cargar_portfolio()
        alertas = cargar_alertas()
        targets_positions = cargar_targets_positions()
        targets_categorias = cargar_targets()
        
        # Cargar activos nuevos planificados
        nuevos_file = os.path.join(DATA_DIR, 'nuevos_activos.json')
        nuevos_activos = {}
        if os.path.exists(nuevos_file):
            with open(nuevos_file, 'r') as f:
                nuevos_activos = json.load(f)
        
        data = portfolio.to_dict()
        
        # Añadir alertas
        data['alertas'] = alertas
        
        # Añadir targets
        data['targets_positions'] = targets_positions
        data['targets_categorias'] = targets_categorias
        data['nuevos_activos'] = nuevos_activos
        
        # Añadir metadatos
        data['export_date'] = datetime.now().isoformat()
        data['export_version'] = '4.0'  # Nueva versión con targets
        
        response = app.response_class(
            response=json.dumps(data, indent=2, ensure_ascii=False),
            status=200,
            mimetype='application/json'
        )
        response.headers['Content-Disposition'] = f'attachment; filename=portfolio_backup_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
        return response
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/import', methods=['POST'])
def api_import():
    """Importa una cartera desde JSON"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No se ha enviado ningún archivo'})
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No se ha seleccionado ningún archivo'})
        
        if not file.filename.endswith('.json'):
            return jsonify({'success': False, 'error': 'El archivo debe ser .json'})
        
        # Leer y parsear JSON
        content = file.read().decode('utf-8')
        data = json.loads(content)
        
        # Validar estructura básica
        if 'posiciones' not in data:
            return jsonify({'success': False, 'error': 'Formato de archivo inválido'})
        
        # Crear portfolio desde datos importados
        portfolio = Portfolio.from_dict(data)
        
        # Guardar portfolio
        guardar_portfolio(portfolio)
        
        # Importar alertas si existen
        alertas_importadas = 0
        if 'alertas' in data and isinstance(data['alertas'], list):
            guardar_alertas(data['alertas'])
            alertas_importadas = len(data['alertas'])
        
        # Importar targets por posición si existen
        targets_importados = 0
        if 'targets_positions' in data and isinstance(data['targets_positions'], dict):
            targets_file = os.path.join(DATA_DIR, 'targets_positions.json')
            with open(targets_file, 'w') as f:
                json.dump(data['targets_positions'], f, indent=2)
            targets_importados = len(data['targets_positions'])
        
        # Importar targets por categoría si existen
        if 'targets_categorias' in data and isinstance(data['targets_categorias'], dict):
            guardar_targets(data['targets_categorias'])
        
        # Importar activos nuevos planificados si existen
        nuevos_importados = 0
        if 'nuevos_activos' in data and isinstance(data['nuevos_activos'], dict):
            nuevos_file = os.path.join(DATA_DIR, 'nuevos_activos.json')
            with open(nuevos_file, 'w') as f:
                json.dump(data['nuevos_activos'], f, indent=2)
            nuevos_importados = len(data['nuevos_activos'])
        
        mensaje = f'Cartera importada correctamente ({len(portfolio.posiciones)} posiciones'
        if alertas_importadas > 0:
            mensaje += f', {alertas_importadas} alertas'
        if targets_importados > 0:
            mensaje += f', {targets_importados} targets'
        if nuevos_importados > 0:
            mensaje += f', {nuevos_importados} activos planificados'
        mensaje += ')'
        
        return jsonify({
            'success': True, 
            'message': mensaje
        })
        
    except json.JSONDecodeError:
        return jsonify({'success': False, 'error': 'El archivo no es un JSON válido'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/import/merge', methods=['POST'])
def api_import_merge():
    """Importa y fusiona con la cartera existente"""
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No se ha enviado ningún archivo'})
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No se ha seleccionado ningún archivo'})
        
        # Leer y parsear JSON
        content = file.read().decode('utf-8')
        data = json.loads(content)
        
        if 'posiciones' not in data:
            return jsonify({'success': False, 'error': 'Formato de archivo inválido'})
        
        # Cargar cartera actual
        portfolio_actual = cargar_portfolio()
        portfolio_importado = Portfolio.from_dict(data)
        
        # Fusionar: añadir posiciones del importado al actual
        posiciones_nuevas = 0
        aportaciones_nuevas = 0
        
        for pos_import in portfolio_importado.posiciones:
            existente = portfolio_actual.buscar_por_isin(pos_import.isin)
            if existente:
                # Añadir aportaciones que no existan
                for ap in pos_import.aportaciones:
                    existente.aportaciones.append(ap)
                    aportaciones_nuevas += 1
            else:
                portfolio_actual.posiciones.append(pos_import)
                posiciones_nuevas += 1
        
        guardar_portfolio(portfolio_actual)
        
        # Fusionar alertas si existen
        alertas_nuevas = 0
        if 'alertas' in data and isinstance(data['alertas'], list):
            alertas_actuales = cargar_alertas()
            ids_existentes = {a.get('id') for a in alertas_actuales}
            
            for alerta_import in data['alertas']:
                # Solo añadir si no existe ya (por ID)
                if alerta_import.get('id') not in ids_existentes:
                    alertas_actuales.append(alerta_import)
                    alertas_nuevas += 1
            
            guardar_alertas(alertas_actuales)
        
        # Fusionar targets por posición si existen
        targets_nuevos = 0
        if 'targets_positions' in data and isinstance(data['targets_positions'], dict):
            targets_actuales = cargar_targets_positions()
            for isin, target in data['targets_positions'].items():
                if isin not in targets_actuales:
                    targets_actuales[isin] = target
                    targets_nuevos += 1
            targets_file = os.path.join(DATA_DIR, 'targets_positions.json')
            with open(targets_file, 'w') as f:
                json.dump(targets_actuales, f, indent=2)
        
        # Fusionar activos nuevos planificados si existen
        nuevos_importados = 0
        if 'nuevos_activos' in data and isinstance(data['nuevos_activos'], dict):
            nuevos_file = os.path.join(DATA_DIR, 'nuevos_activos.json')
            nuevos_actuales = {}
            if os.path.exists(nuevos_file):
                with open(nuevos_file, 'r') as f:
                    nuevos_actuales = json.load(f)
            
            for isin, info in data['nuevos_activos'].items():
                if isin not in nuevos_actuales:
                    nuevos_actuales[isin] = info
                    nuevos_importados += 1
            
            with open(nuevos_file, 'w') as f:
                json.dump(nuevos_actuales, f, indent=2)
        
        mensaje = f'Fusión completada: {posiciones_nuevas} posiciones nuevas, {aportaciones_nuevas} aportaciones añadidas'
        if alertas_nuevas > 0:
            mensaje += f', {alertas_nuevas} alertas'
        if targets_nuevos > 0:
            mensaje += f', {targets_nuevos} targets'
        if nuevos_importados > 0:
            mensaje += f', {nuevos_importados} activos planificados'
        
        return jsonify({
            'success': True, 
            'message': mensaje
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/settings')
@login_required
def settings_page():
    """Página de configuración"""
    return render_template('settings.html')


@app.route('/api/benchmarks')
def api_benchmarks():
    """Obtiene rendimiento de benchmarks para comparar"""
    periodo = request.args.get('periodo', '1y')
    
    benchmarks = {
        'SP500': {'ticker': '^GSPC', 'nombre': 'S&P 500'},
        'MSCI_WORLD': {'ticker': 'URTH', 'nombre': 'MSCI World'},  # ETF que replica MSCI World
        'IBEX35': {'ticker': '^IBEX', 'nombre': 'IBEX 35'},
        'NASDAQ': {'ticker': '^IXIC', 'nombre': 'NASDAQ'},
    }
    
    resultados = {}
    
    for key, info in benchmarks.items():
        try:
            import yfinance as yf
            ticker = yf.Ticker(info['ticker'])
            hist = ticker.history(period=periodo)
            
            if not hist.empty:
                precio_inicio = hist['Close'].iloc[0]
                precio_fin = hist['Close'].iloc[-1]
                rentabilidad = ((precio_fin - precio_inicio) / precio_inicio) * 100
                
                resultados[key] = {
                    'nombre': info['nombre'],
                    'rentabilidad': round(rentabilidad, 2),
                    'precio_actual': round(precio_fin, 2)
                }
        except Exception as e:
            pass
    
    # Calcular rentabilidad de la cartera usando el MISMO método que evolution
    mi_rentabilidad = calcular_rentabilidad_cartera(periodo)
    
    return jsonify({
        'success': True, 
        'data': resultados,
        'mi_cartera': {
            'rentabilidad': round(mi_rentabilidad, 2),
            'periodo': periodo
        }
    })


def calcular_rentabilidad_cartera(periodo: str) -> float:
    """
    Calcula la rentabilidad de la cartera para un período dado.
    Usa el MISMO método que api_portfolio_evolution para consistencia.
    """
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return 0
    
    try:
        import yfinance as yf
        from datetime import datetime, timedelta
        from src.scrapers import JustETFScraper
        
        justetf = JustETFScraper()
        
        # Calcular fecha de inicio según período
        if periodo == 'ytd':
            fecha_inicio = datetime(datetime.now().year, 1, 1)
            periodo_api = '1y'  # YTD usa período de 1 año en API
        else:
            dias_map = {'1mo': 30, '3mo': 90, '6mo': 180, '1y': 365, '2y': 730, '5y': 1825}
            dias = dias_map.get(periodo, 365)
            fecha_inicio = datetime.now() - timedelta(days=dias)
            periodo_api = periodo
        
        # Encontrar fecha de primera compra
        fecha_primera_compra = None
        for pos in portfolio.posiciones:
            for ap in pos.aportaciones:
                if ap.fecha_compra:
                    try:
                        fecha_ap = datetime.strptime(ap.fecha_compra, '%Y-%m-%d')
                        if fecha_primera_compra is None or fecha_ap < fecha_primera_compra:
                            fecha_primera_compra = fecha_ap
                    except:
                        pass
        
        # Si la fecha de primera compra es después del inicio del período, usar esa
        if fecha_primera_compra and fecha_primera_compra > fecha_inicio:
            fecha_inicio = fecha_primera_compra
        
        fecha_inicio_str = fecha_inicio.strftime('%Y-%m-%d')
        
        # Obtener histórico de cada posición
        all_data = {}
        
        for pos in portfolio.posiciones:
            hist_data = None
            
            # 1. Intentar con Yahoo Finance si hay ticker
            if pos.ticker:
                try:
                    ticker = yf.Ticker(pos.ticker)
                    hist = ticker.history(period=periodo_api)
                    if not hist.empty:
                        hist_data = {
                            'fechas': {d.strftime('%Y-%m-%d'): hist.loc[d, 'Close'] for d in hist.index},
                            'cantidad': pos.cantidad
                        }
                except:
                    pass
            
            # 2. Si no hay datos de Yahoo, intentar con justETF
            if not hist_data and pos.isin:
                try:
                    historico = justetf.obtener_historico(pos.isin, periodo_api)
                    if historico and historico.get('precios'):
                        hist_data = {
                            'fechas': dict(zip(historico['fechas'], historico['precios'])),
                            'cantidad': pos.cantidad
                        }
                except:
                    pass
            
            if hist_data:
                all_data[pos.id] = hist_data
        
        if not all_data:
            return 0
        
        # Encontrar todas las fechas únicas después del inicio
        all_fechas = set()
        for data in all_data.values():
            all_fechas.update(data['fechas'].keys())
        
        fechas_filtradas = [f for f in all_fechas if f >= fecha_inicio_str]
        fechas_ordenadas = sorted(fechas_filtradas)
        
        if not fechas_ordenadas:
            return 0
        
        # Calcular valor con interpolación
        ultimo_precio = {pos_id: None for pos_id in all_data.keys()}
        
        # Valor al inicio del período
        valor_inicio = 0
        for pos_id, data in all_data.items():
            for fecha in fechas_ordenadas:
                if fecha in data['fechas']:
                    precio = data['fechas'][fecha]
                    valor_inicio += precio * data['cantidad']
                    ultimo_precio[pos_id] = precio
                    break
                elif ultimo_precio[pos_id]:
                    valor_inicio += ultimo_precio[pos_id] * data['cantidad']
                    break
        
        # Valor actual (usar precio real actualizado)
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones_actualizadas = analyzer.actualizar_precios()
        valor_actual = sum(p.valor_actual for p in posiciones_actualizadas)
        
        # Calcular rentabilidad
        if valor_inicio > 0:
            return ((valor_actual - valor_inicio) / valor_inicio) * 100
        
        return 0
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return 0


@app.route('/api/portfolio/evolution')
def api_portfolio_evolution():
    """Calcula la evolución histórica REAL de la cartera, considerando las fechas de cada compra"""
    periodo = request.args.get('periodo', '1y')
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        import yfinance as yf
        from datetime import datetime, timedelta
        from src.scrapers import JustETFScraper
        
        justetf = JustETFScraper()
        
        # Construir historial de aportaciones por posición
        # Formato: {pos_id: [(fecha_compra, cantidad), ...]}
        aportaciones_por_posicion = {}
        fecha_primera_compra = None
        
        for pos in portfolio.posiciones:
            aportaciones_pos = []
            for ap in pos.aportaciones:
                if ap.fecha_compra and ap.cantidad:
                    try:
                        fecha_ap = datetime.strptime(ap.fecha_compra, '%Y-%m-%d')
                        aportaciones_pos.append((ap.fecha_compra, ap.cantidad))
                        if fecha_primera_compra is None or fecha_ap < fecha_primera_compra:
                            fecha_primera_compra = fecha_ap
                    except:
                        pass
            # Ordenar por fecha
            aportaciones_pos.sort(key=lambda x: x[0])
            aportaciones_por_posicion[pos.id] = aportaciones_pos
        
        if not fecha_primera_compra:
            fecha_primera_compra = datetime.now() - timedelta(days=365)
        
        # Si el período es "max", usar desde la primera compra
        fecha_inicio_filtro = None
        if periodo == 'max':
            fecha_inicio_filtro = fecha_primera_compra
            dias = (datetime.now() - fecha_primera_compra).days
            if dias <= 30:
                periodo_api = '1mo'
            elif dias <= 90:
                periodo_api = '3mo'
            elif dias <= 180:
                periodo_api = '6mo'
            elif dias <= 365:
                periodo_api = '1y'
            elif dias <= 730:
                periodo_api = '2y'
            else:
                periodo_api = '5y'
        else:
            periodo_api = periodo
            dias_map = {'1mo': 30, '3mo': 90, '6mo': 180, '1y': 365, '2y': 730, '5y': 1825}
            dias = dias_map.get(periodo, 365)
            fecha_inicio_filtro = datetime.now() - timedelta(days=dias)
            
            if fecha_primera_compra > fecha_inicio_filtro:
                fecha_inicio_filtro = fecha_primera_compra
        
        # Obtener histórico de precios de cada posición
        all_data = {}
        
        for pos in portfolio.posiciones:
            hist_data = None
            
            # 1. Intentar con Yahoo Finance
            if pos.ticker:
                try:
                    ticker = yf.Ticker(pos.ticker)
                    hist = ticker.history(period=periodo_api)
                    if not hist.empty:
                        hist_data = {
                            'fechas': {d.strftime('%Y-%m-%d'): hist.loc[d, 'Close'] for d in hist.index},
                            'aportaciones': aportaciones_por_posicion.get(pos.id, []),
                            'nombre': pos.nombre
                        }
                except:
                    pass
            
            # 2. Si no hay datos de Yahoo, intentar con justETF
            if not hist_data and pos.isin:
                try:
                    historico = justetf.obtener_historico(pos.isin, periodo_api)
                    if historico and historico.get('precios'):
                        hist_data = {
                            'fechas': dict(zip(historico['fechas'], historico['precios'])),
                            'aportaciones': aportaciones_por_posicion.get(pos.id, []),
                            'nombre': pos.nombre
                        }
                except:
                    pass
            
            if hist_data:
                all_data[pos.id] = hist_data
        
        if not all_data:
            return jsonify({'success': False, 'error': 'No hay datos históricos. Verifica que tus posiciones tienen ISIN válido.'})
        
        # Encontrar todas las fechas únicas de precios
        all_fechas = set()
        for data in all_data.values():
            all_fechas.update(data['fechas'].keys())
        
        fecha_inicio_str = fecha_inicio_filtro.strftime('%Y-%m-%d')
        fechas_filtradas = [f for f in all_fechas if f >= fecha_inicio_str]
        fechas_ordenadas = sorted(fechas_filtradas)
        
        if not fechas_ordenadas:
            return jsonify({'success': False, 'error': 'No hay datos para el período seleccionado'})
        
        # Función helper: calcular cantidad que tenía en una fecha específica
        def cantidad_en_fecha(aportaciones, fecha_str):
            """Suma las cantidades de aportaciones anteriores o iguales a la fecha"""
            total = 0
            for fecha_compra, cantidad in aportaciones:
                if fecha_compra <= fecha_str:
                    total += cantidad
            return total
        
        # Calcular valor REAL de cartera para cada fecha
        valores_cartera = []
        fechas_str = []
        ultimo_precio_conocido = {pos_id: None for pos_id in all_data.keys()}
        
        for fecha in fechas_ordenadas:
            valor_total = 0
            hay_alguna_posicion = False
            
            for pos_id, data in all_data.items():
                # Calcular cuántas unidades tenía EN ESTA FECHA
                cantidad_actual = cantidad_en_fecha(data['aportaciones'], fecha)
                
                # Si aún no había comprado esta posición, no sumar nada
                if cantidad_actual <= 0:
                    continue
                
                hay_alguna_posicion = True
                
                # Obtener precio (actual o último conocido)
                if fecha in data['fechas']:
                    precio = data['fechas'][fecha]
                    ultimo_precio_conocido[pos_id] = precio
                elif ultimo_precio_conocido[pos_id] is not None:
                    precio = ultimo_precio_conocido[pos_id]
                else:
                    continue
                
                valor_total += precio * cantidad_actual
            
            # Solo incluir fechas donde tenía al menos una posición
            if hay_alguna_posicion and valor_total > 0:
                valores_cartera.append(round(valor_total, 2))
                fechas_str.append(fecha)
        
        if not valores_cartera:
            return jsonify({
                'success': False, 
                'error': 'No hay suficientes datos históricos. Algunas posiciones no tienen histórico disponible.',
                'posiciones_sin_datos': [pos.nombre for pos in portfolio.posiciones if pos.id not in all_data]
            })
        
        # Obtener el valor ACTUAL REAL del portfolio (no del histórico)
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones_actualizadas = analyzer.actualizar_precios()
        valor_actual_real = sum(p.valor_actual for p in posiciones_actualizadas)
        coste_total_real = sum(pos.coste_total for pos in portfolio.posiciones)
        
        # Usar el valor actual real, no el último del histórico
        # (el histórico puede estar desactualizado)
        valor_inicio_periodo = valores_cartera[0] if valores_cartera else 0
        valor_final_historico = valores_cartera[-1] if valores_cartera else 0
        
        # Añadir el punto actual si es diferente del último histórico
        # para que el gráfico llegue hasta el valor real
        if abs(valor_actual_real - valor_final_historico) > 1:
            from datetime import datetime
            hoy = datetime.now().strftime('%Y-%m-%d')
            if hoy not in fechas_str:
                fechas_str.append(hoy)
                valores_cartera.append(round(valor_actual_real, 2))
        
        # Rentabilidad del período seleccionado
        if valor_inicio_periodo > 0:
            rentabilidad_periodo = ((valor_actual_real - valor_inicio_periodo) / valor_inicio_periodo) * 100
        else:
            rentabilidad_periodo = 0
        
        # Rentabilidad total (vs lo invertido)
        if coste_total_real > 0:
            rentabilidad_total = ((valor_actual_real - coste_total_real) / coste_total_real) * 100
        else:
            rentabilidad_total = 0
        
        # Verificar si hay posiciones sin datos históricos
        posiciones_sin_historico = [pos.nombre for pos in portfolio.posiciones if pos.id not in all_data]
        
        return jsonify({
            'success': True,
            'data': {
                'fechas': fechas_str,
                'valores': valores_cartera,
                'rentabilidad': round(rentabilidad_periodo, 2),  # Del período seleccionado
                'rentabilidad_total': round(rentabilidad_total, 2),  # Total vs invertido
                'valor_inicial': round(valor_inicio_periodo, 2),  # Valor al inicio del período
                'valor_final': round(valor_actual_real, 2),  # Valor ACTUAL real
                'coste_invertido': round(coste_total_real, 2),
                'periodo_usado': periodo_api,
                'fecha_primera_compra': fecha_primera_compra.strftime('%Y-%m-%d'),
                'num_activos_con_datos': len(all_data),
                'num_activos_total': len(portfolio.posiciones),
                'posiciones_sin_historico': posiciones_sin_historico
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/heatmap')
@login_required
def heatmap_page():
    """Página del mapa de calor a pantalla completa"""
    return render_template('heatmap.html')


@app.route('/api/portfolio/heatmap')
@login_required
def api_portfolio_heatmap():
    """Obtiene datos para el mapa de calor con cambio % por período"""
    periodo = request.args.get('periodo', '1d')  # 1d, 1w, 1m, ytd
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        import yfinance as yf
        from datetime import datetime, timedelta
        from src.scrapers import JustETFScraper, obtener_cambio_diario_justetf, obtener_cambios_diarios_batch, obtener_cambio_diario_yahoo, obtener_cambio_diario_con_info
        
        justetf = JustETFScraper()
        
        # Mapeo de períodos a días hacia atrás y períodos de justETF
        period_days = {
            '1d': 1,
            '1w': 7,
            '1m': 30,
            'ytd': (datetime.now() - datetime(datetime.now().year, 1, 1)).days
        }
        justetf_periods = {
            '1d': '1mo',  # justETF mínimo 1 mes
            '1w': '1mo',
            '1m': '1mo',
            'ytd': '1y'
        }
        days_back = period_days.get(periodo, 1)
        
        # Actualizar precios actuales
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones_actualizadas = analyzer.actualizar_precios()
        
        # Calcular valor total para pesos
        valor_total = sum(p.valor_actual for p in posiciones_actualizadas)
        
        heatmap_data = []
        
        # Para periodo 1d, obtener cambios de JustETF en batch (más eficiente)
        cambios_justetf = {}
        mensaje_cierre_global = None
        if periodo == '1d':
            isins_etf = [p.isin for p in portfolio.posiciones if p.isin and p.isin[:2] in ['IE', 'LU', 'DE', 'FR', 'NL', 'GB']]
            if isins_etf:
                print(f"[Heatmap] Obteniendo cambios diarios de JustETF para {len(isins_etf)} ETFs...")
                try:
                    cambios_justetf = obtener_cambios_diarios_batch(isins_etf)
                except Exception as e:
                    print(f"[Heatmap] Error en batch JustETF: {e}")
            
            # Generar mensaje de cierre global usando la primera posición como referencia
            ahora = datetime.now()
            es_fin_de_semana = ahora.weekday() >= 5
            hora_actual = ahora.hour
            fuera_horario = hora_actual < 8 or hora_actual >= 22
            meses = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
            
            # Intentar obtener fecha real de la primera posición
            primera_pos = next((p for p in portfolio.posiciones if p.isin and p.isin[:2] in ['IE', 'LU', 'DE', 'FR', 'NL', 'GB']), None)
            if primera_pos:
                try:
                    info = obtener_cambio_diario_con_info(primera_pos.isin, primera_pos.ticker)
                    if info and info.get('mensaje'):
                        mensaje_cierre_global = info['mensaje']
                except:
                    pass
            
            # Si no se obtuvo mensaje, generar uno por defecto
            if not mensaje_cierre_global and (es_fin_de_semana or fuera_horario):
                if es_fin_de_semana:
                    dias_desde_viernes = ahora.weekday() - 4
                    if dias_desde_viernes < 0:
                        dias_desde_viernes += 7
                    ultimo_dia = ahora - timedelta(days=dias_desde_viernes)
                    mensaje_cierre_global = f"Al cierre: {ultimo_dia.day} {meses[ultimo_dia.month-1]} {ultimo_dia.year} · Mercado cerrado"
                else:
                    mensaje_cierre_global = f"Al cierre: {ahora.day} {meses[ahora.month-1]} {ahora.year} · Mercado cerrado"
        
        for pos_actual in posiciones_actualizadas:
            # Buscar posición original
            pos_original = next((p for p in portfolio.posiciones if p.id == pos_actual.id), None)
            if not pos_original:
                continue
            
            cambio_pct = 0
            datos_encontrados = False
            
            # Para ETFs europeos (ISIN IE/LU/DE/FR/NL/GB)
            es_etf_europeo = pos_original.isin and pos_original.isin[:2] in ['IE', 'LU', 'DE', 'FR', 'NL', 'GB']
            
            # ============================================================
            # PERIODO 1D: Comparar JustETF y Yahoo, usar el más reciente
            # ============================================================
            if periodo == '1d':
                es_crypto = pos_original.categoria and 'crypto' in pos_original.categoria.lower()
                
                # 1. Para ETFs europeos: comparar JustETF y Yahoo, usar el más reciente
                if es_etf_europeo and pos_original.isin:
                    try:
                        # Usar la función que compara ambas fuentes
                        info_cambio = obtener_cambio_diario_con_info(pos_original.isin, pos_original.ticker)
                        if info_cambio and info_cambio.get('cambio') is not None:
                            cambio_pct = info_cambio['cambio']
                            datos_encontrados = True
                            fuente = info_cambio.get('fuente', 'unknown')
                            print(f"[Heatmap] {fuente.upper()} para {pos_original.isin}: {cambio_pct:.2f}%")
                    except Exception as e:
                        print(f"[Heatmap] Error comparando fuentes para {pos_original.isin}: {e}")
                        # Fallback al batch de JustETF
                        if pos_original.isin in cambios_justetf and cambios_justetf[pos_original.isin] is not None:
                            cambio_pct = cambios_justetf[pos_original.isin]
                            datos_encontrados = True
                            print(f"[Heatmap] JustETF batch fallback para {pos_original.isin}: {cambio_pct}%")
                
                # 2. Si es crypto y el cambio es 0% (mercado cerrado), usar BTC-USD como aproximación
                if es_crypto and datos_encontrados and cambio_pct == 0:
                    try:
                        cambio_btc = obtener_cambio_diario_yahoo('BTC-USD')
                        if cambio_btc is not None and cambio_btc != 0:
                            cambio_pct = cambio_btc
                            print(f"[Heatmap] Crypto {pos_original.isin} mercado cerrado, usando BTC-USD: {cambio_pct:.2f}%")
                    except Exception as e:
                        print(f"[Heatmap] Error BTC-USD fallback: {e}")
                
                # 3. Para otros activos: usar Yahoo Finance con lógica mejorada
                if not datos_encontrados and pos_original.ticker:
                    try:
                        cambio = obtener_cambio_diario_yahoo(pos_original.ticker)
                        if cambio is not None:
                            cambio_pct = cambio
                            datos_encontrados = True
                            print(f"[Heatmap] Yahoo para {pos_original.ticker}: {cambio_pct:.2f}%")
                    except Exception as e:
                        print(f"[Heatmap] Yahoo error para {pos_original.ticker}: {e}")
            
            # ============================================================
            # OTROS PERIODOS (1w, 1m, ytd): Usar histórico
            # ============================================================
            else:
                # 1. Si es ETF europeo, intentar JustETF primero
                if es_etf_europeo and pos_original.isin:
                    try:
                        justetf_periodo = justetf_periods.get(periodo, '1mo')
                        historico = justetf.obtener_historico(pos_original.isin, justetf_periodo)
                        
                        if historico and historico.get('precios') and len(historico['precios']) > 1:
                            precios = historico['precios']
                            fechas = historico.get('fechas', [])
                            
                            # Encontrar precio de inicio según período
                            if periodo == 'ytd':
                                year_start = f"{datetime.now().year}-01"
                                precio_inicio = precios[0]
                                for i, fecha in enumerate(fechas):
                                    if fecha.startswith(year_start):
                                        precio_inicio = precios[i]
                                        break
                            elif periodo == '1m':
                                precio_inicio = precios[0]
                            elif periodo == '1w':
                                idx = max(0, len(precios) - 5)
                                precio_inicio = precios[idx]
                            else:
                                precio_inicio = precios[-2] if len(precios) >= 2 else precios[0]
                            
                            precio_actual = precios[-1]
                            if precio_inicio > 0:
                                cambio_pct = ((precio_actual - precio_inicio) / precio_inicio) * 100
                                datos_encontrados = True
                                print(f"[Heatmap] JustETF historico para {pos_original.isin}: {cambio_pct:.2f}%")
                    except Exception as e:
                        print(f"[Heatmap] JustETF historico error para {pos_original.isin}: {e}")
                
                # 2. Si no es ETF europeo o JustETF falló, usar Yahoo Finance
                if not datos_encontrados and pos_original.ticker:
                    try:
                        ticker = yf.Ticker(pos_original.ticker)
                        
                        if periodo == 'ytd':
                            start_date = datetime(datetime.now().year, 1, 1)
                            hist = ticker.history(start=start_date)
                        else:
                            hist = ticker.history(period=f'{days_back}d')
                        
                        if not hist.empty and len(hist) > 0:
                            precio_inicio = float(hist['Close'].iloc[0])
                            precio_fin = float(hist['Close'].iloc[-1])
                            if precio_inicio > 0 and precio_fin > 0:
                                cambio_pct = ((precio_fin - precio_inicio) / precio_inicio) * 100
                                datos_encontrados = True
                                print(f"[Heatmap] Yahoo OK para {pos_original.ticker}: {cambio_pct:.2f}%")
                    except Exception as e:
                        print(f"[Heatmap] Yahoo error para {pos_original.ticker}: {e}")
                
                # 3. Fallback final: justETF si hay ISIN
                if not datos_encontrados and pos_original.isin:
                    print(f"[Heatmap] Fallback justETF para {pos_original.isin}...")
                    try:
                        justetf_periodo = justetf_periods.get(periodo, '1mo')
                        historico = justetf.obtener_historico(pos_original.isin, justetf_periodo)
                        
                        if historico and historico.get('precios') and len(historico['precios']) > 1:
                            precios = historico['precios']
                            fechas = historico.get('fechas', [])
                            
                            if periodo == 'ytd':
                                year_start = f"{datetime.now().year}-01"
                                precio_inicio = precios[0]
                                for i, fecha in enumerate(fechas):
                                    if fecha.startswith(year_start):
                                        precio_inicio = precios[i]
                                        break
                            elif periodo == '1m':
                                precio_inicio = precios[0]
                            elif periodo == '1w':
                                idx = max(0, len(precios) - 5)
                                precio_inicio = precios[idx]
                            else:
                                precio_inicio = precios[-2] if len(precios) >= 2 else precios[0]
                            
                            precio_actual = precios[-1]
                            if precio_inicio > 0:
                                cambio_pct = ((precio_actual - precio_inicio) / precio_inicio) * 100
                                datos_encontrados = True
                                print(f"[Heatmap] justETF fallback OK para {pos_original.isin}: {cambio_pct:.2f}%")
                    except Exception as e:
                        print(f"[Heatmap] justETF fallback error para {pos_original.isin}: {e}")
            
            # Calcular peso en cartera
            peso = (pos_actual.valor_actual / valor_total * 100) if valor_total > 0 else 0
            
            heatmap_data.append({
                'id': pos_original.id,
                'ticker': pos_original.ticker or pos_original.isin[:6],
                'nombre': pos_original.nombre,
                'categoria': pos_original.categoria or '',
                'valor': round(pos_actual.valor_actual, 2),
                'cantidad': pos_original.cantidad,
                'cambio': round(cambio_pct, 2),
                'peso': round(peso, 2),
                'sin_datos': not datos_encontrados
            })
        
        # Ordenar por valor (mayor a menor)
        heatmap_data.sort(key=lambda x: x['valor'], reverse=True)
        
        return jsonify({
            'success': True,
            'data': heatmap_data,
            'periodo': periodo,
            'valor_total': round(valor_total, 2),
            'mensaje_cierre': mensaje_cierre_global
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/sankey')
@login_required
def api_portfolio_sankey():
    """Obtiene datos para el diagrama Sankey de diversificación"""
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        import yfinance as yf
        
        # Actualizar precios
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones_actualizadas = analyzer.actualizar_precios()
        
        # Calcular valor total
        valor_total = sum(p.valor_actual for p in posiciones_actualizadas)
        
        if valor_total <= 0:
            return jsonify({'success': False, 'error': 'Valor total es 0'})
        
        # Estructura para almacenar los datos del Sankey
        sankey_data = []
        
        # Agrupar posiciones por categoría
        categorias = {}
        
        for pos_actual in posiciones_actualizadas:
            # Buscar posición original
            pos_original = next((p for p in portfolio.posiciones if p.id == pos_actual.id), None)
            if not pos_original:
                continue
            
            categoria = pos_original.categoria or 'Sin categoría'
            valor = pos_actual.valor_actual
            
            if categoria not in categorias:
                categorias[categoria] = {
                    'valor': 0,
                    'posiciones': []
                }
            
            categorias[categoria]['valor'] += valor
            
            # Intentar obtener sector/industria de Yahoo Finance para acciones
            sector = None
            industry = None
            
            if pos_original.ticker and not pos_original.isin:
                # Probablemente es una acción, intentar obtener sector
                try:
                    ticker_yf = yf.Ticker(pos_original.ticker)
                    info = ticker_yf.info
                    sector = info.get('sector')
                    industry = info.get('industry')
                except:
                    pass
            
            # Si no hay sector de Yahoo, usar la categoría como sector
            if not sector:
                sector = categoria
            
            # Si no hay industry, usar la categoría
            if not industry:
                industry = categoria
            
            # Usar nombre descriptivo en lugar de ticker
            nombre_corto = pos_original.nombre
            # Acortar nombres muy largos
            if len(nombre_corto) > 25:
                nombre_corto = nombre_corto[:22] + '...'
            
            categorias[categoria]['posiciones'].append({
                'nombre': nombre_corto,
                'ticker': pos_original.ticker or pos_original.isin[:12] if pos_original.isin else 'N/A',
                'valor': valor,
                'sector': sector,
                'industry': industry
            })
        
        # Determinar umbral para agrupar posiciones pequeñas
        num_posiciones = len(posiciones_actualizadas)
        # Si hay más de 10 posiciones, agrupar las menores al 2%
        # Si hay más de 20, agrupar las menores al 3%
        if num_posiciones > 20:
            umbral_agrupacion = 3.0
        elif num_posiciones > 10:
            umbral_agrupacion = 2.0
        else:
            umbral_agrupacion = 0.5  # Casi sin agrupar
        
        # Construir datos del Sankey CON PORCENTAJES Y NOMBRES DESCRIPTIVOS
        # Nivel 1: Portfolio → Categoría
        for cat, cat_data in categorias.items():
            peso = (cat_data['valor'] / valor_total) * 100
            if peso >= 0.5:  # Solo incluir si es >= 0.5%
                cat_label = f"{cat} ({peso:.1f}%)"
                sankey_data.append(['Portfolio', cat_label, cat_data['valor']])
        
        # Agrupar por sector dentro de cada categoría
        for cat, cat_data in categorias.items():
            peso_cat = (cat_data['valor'] / valor_total) * 100
            if peso_cat < 0.5:
                continue
                
            cat_label = f"{cat} ({peso_cat:.1f}%)"
            
            # Separar posiciones grandes y pequeñas
            posiciones_grandes = []
            valor_otros = 0
            num_otros = 0
            
            for pos in cat_data['posiciones']:
                peso_pos = (pos['valor'] / valor_total) * 100
                if peso_pos >= umbral_agrupacion:
                    posiciones_grandes.append(pos)
                else:
                    valor_otros += pos['valor']
                    num_otros += 1
            
            # Añadir posiciones grandes
            for pos in posiciones_grandes:
                peso_pos = (pos['valor'] / valor_total) * 100
                nombre_label = f"{pos['nombre']} ({peso_pos:.1f}%)"
                sankey_data.append([cat_label, nombre_label, pos['valor']])
            
            # Añadir "Otros" si hay posiciones agrupadas
            if valor_otros > 0 and num_otros > 0:
                peso_otros = (valor_otros / valor_total) * 100
                otros_label = f"Otros ({num_otros}) ({peso_otros:.1f}%)"
                sankey_data.append([cat_label, otros_label, valor_otros])
        
        # Crear resumen de categorías
        resumen = []
        for cat, cat_data in sorted(categorias.items(), key=lambda x: x[1]['valor'], reverse=True):
            porcentaje = (cat_data['valor'] / valor_total) * 100
            resumen.append({
                'categoria': cat,
                'valor': round(cat_data['valor'], 2),
                'porcentaje': round(porcentaje, 1),
                'num_posiciones': len(cat_data['posiciones'])
            })
        
        # Calcular altura recomendada según número de nodos
        num_nodos_finales = len(set([row[1] for row in sankey_data]))
        altura_recomendada = max(380, min(700, 50 + num_nodos_finales * 35))
        
        return jsonify({
            'success': True,
            'data': {
                'sankey': sankey_data,
                'resumen': resumen,
                'valor_total': round(valor_total, 2),
                'num_posiciones': len(posiciones_actualizadas),
                'num_categorias': len(categorias),
                'altura_recomendada': altura_recomendada
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/positions-evolution')
def api_positions_evolution():
    """Obtiene la evolución de cada posición individual para comparar"""
    periodo = request.args.get('periodo', '6mo')
    normalized = request.args.get('normalized', 'false') == 'true'
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        import yfinance as yf
        from datetime import datetime
        from src.scrapers import JustETFScraper
        
        justetf = JustETFScraper()
        
        # Si el período es "max", calcular desde la primera fecha de compra
        if periodo == 'max':
            fecha_inicio = None
            for pos in portfolio.posiciones:
                for ap in pos.aportaciones:
                    if ap.fecha_compra:
                        try:
                            fecha_ap = datetime.strptime(ap.fecha_compra, '%Y-%m-%d')
                            if fecha_inicio is None or fecha_ap < fecha_inicio:
                                fecha_inicio = fecha_ap
                        except:
                            pass
            
            if fecha_inicio:
                dias = (datetime.now() - fecha_inicio).days
                if dias <= 30:
                    periodo = '1mo'
                elif dias <= 90:
                    periodo = '3mo'
                elif dias <= 180:
                    periodo = '6mo'
                elif dias <= 365:
                    periodo = '1y'
                elif dias <= 730:
                    periodo = '2y'
                elif dias <= 1825:
                    periodo = '5y'
                else:
                    periodo = '10y'
        
        posiciones_data = []
        sin_datos = []
        fechas_comunes = None
        
        for pos in portfolio.posiciones:
            fechas = None
            precios = None
            fuente = None
            
            # 1. Intentar con Yahoo Finance si hay ticker
            if pos.ticker:
                try:
                    ticker = yf.Ticker(pos.ticker)
                    hist = ticker.history(period=periodo)
                    
                    if not hist.empty:
                        fechas = [d.strftime('%Y-%m-%d') for d in hist.index]
                        precios = hist['Close'].tolist()
                        fuente = 'Yahoo Finance'
                except:
                    pass
            
            # 2. Si no hay datos de Yahoo, intentar con justETF usando ISIN
            if not precios and pos.isin:
                try:
                    historico = justetf.obtener_historico(pos.isin, periodo)
                    if historico and historico.get('precios'):
                        fechas = historico['fechas']
                        precios = historico['precios']
                        fuente = 'justETF'
                except:
                    pass
            
            # 3. Si aún no hay datos, añadir a la lista de sin datos
            if not precios:
                sin_datos.append({
                    'nombre': pos.nombre,
                    'isin': pos.isin,
                    'ticker': pos.ticker or 'Sin ticker'
                })
                continue
            
            # Normalizar a base 100 si se pide
            if normalized and precios:
                precio_base = precios[0]
                precios = [(p / precio_base) * 100 for p in precios]
            
            # Calcular rentabilidad del período
            if len(precios) >= 2:
                rent = ((precios[-1] - precios[0]) / precios[0]) * 100 if not normalized else precios[-1] - 100
            else:
                rent = 0
            
            posiciones_data.append({
                'id': pos.id,
                'nombre': pos.nombre[:25] + '...' if len(pos.nombre) > 25 else pos.nombre,
                'nombre_completo': pos.nombre,
                'ticker': pos.ticker,
                'isin': pos.isin,
                'fechas': fechas,
                'precios': [round(p, 2) for p in precios],
                'rentabilidad': round(rent, 2),
                'categoria': pos.categoria,
                'fuente': fuente
            })
            
            # Guardar fechas comunes (usar las del primer activo válido)
            if fechas_comunes is None:
                fechas_comunes = fechas
        
        return jsonify({
            'success': True,
            'data': {
                'posiciones': posiciones_data,
                'fechas': fechas_comunes,
                'normalized': normalized,
                'sin_datos': sin_datos,
                'total_posiciones': len(portfolio.posiciones),
                'posiciones_con_datos': len(posiciones_data)
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/categories')
def api_portfolio_categories():
    """Obtiene distribución por categorías"""
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': True, 'data': {'categorias': {}, 'sin_categoria': 0}})
    
    analyzer = PortfolioAnalyzer(portfolio)
    posiciones = analyzer.actualizar_precios()
    
    # Agrupar por categoría
    categorias = {}
    sin_categoria = 0
    
    for pos in posiciones:
        cat = pos.categoria if pos.categoria else 'Sin categoría'
        if cat not in categorias:
            categorias[cat] = {
                'valor': 0,
                'coste': 0,
                'posiciones': []
            }
        categorias[cat]['valor'] += pos.valor_actual
        categorias[cat]['coste'] += pos.coste_total
        categorias[cat]['posiciones'].append({
            'nombre': pos.nombre,
            'valor': pos.valor_actual,
            'rentabilidad': pos.rentabilidad_pct
        })
    
    # Calcular porcentajes y rentabilidad por categoría
    valor_total = sum(c['valor'] for c in categorias.values())
    
    resultado = {}
    for cat, data in categorias.items():
        peso = (data['valor'] / valor_total * 100) if valor_total > 0 else 0
        rentabilidad = ((data['valor'] - data['coste']) / data['coste'] * 100) if data['coste'] > 0 else 0
        
        resultado[cat] = {
            'valor': round(data['valor'], 2),
            'coste': round(data['coste'], 2),
            'peso': round(peso, 2),
            'rentabilidad': round(rentabilidad, 2),
            'num_posiciones': len(data['posiciones']),
            'posiciones': data['posiciones']
        }
    
    return jsonify({'success': True, 'data': resultado})


@app.route('/api/position/update/<position_id>', methods=['POST'])
def api_update_position(position_id):
    """Actualiza datos de una posición (categoría, ticker, etc)"""
    data = request.get_json()
    
    try:
        portfolio = cargar_portfolio()
        pos = portfolio.obtener_posicion(position_id)
        
        if not pos:
            return jsonify({'success': False, 'error': 'Posición no encontrada'})
        
        # Actualizar campos permitidos
        if 'categoria' in data:
            pos.categoria = data['categoria']
        if 'sector' in data:
            pos.sector = data['sector']
        if 'ticker' in data:
            pos.ticker = data['ticker'].upper().strip()
        if 'nombre' in data:
            pos.nombre = data['nombre']
        
        guardar_portfolio(portfolio)
        
        return jsonify({'success': True, 'message': 'Posición actualizada'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# Lista de categorías predefinidas
CATEGORIAS = [
    'Tecnología',
    'Salud',
    'Finanzas',
    'Energía',
    'Consumo',
    'Industrial',
    'Materiales',
    'Inmobiliario',
    'Comunicaciones',
    'Utilities',
    'Renta Fija',
    'Mercados Emergentes',
    'Global/Diversificado',
    'Oro/Materias Primas',
    'Crypto',
    'Small Caps',
    'Otros'
]

# Archivo para categorías personalizadas
CUSTOM_CATEGORIES_FILE = os.path.join(DATA_DIR, 'custom_categories.json')

def cargar_categorias_custom():
    """Carga las categorías personalizadas"""
    if os.path.exists(CUSTOM_CATEGORIES_FILE):
        try:
            with open(CUSTOM_CATEGORIES_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {'categories': [], 'keywords': {}}

def guardar_categorias_custom(data):
    """Guarda las categorías personalizadas"""
    DATA_DIR.mkdir(exist_ok=True)
    with open(CUSTOM_CATEGORIES_FILE, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def obtener_todas_categorias():
    """Obtiene todas las categorías (predefinidas + custom)"""
    custom = cargar_categorias_custom()
    todas = CATEGORIAS.copy()
    for cat in custom.get('categories', []):
        if cat not in todas:
            todas.insert(-1, cat)  # Insertar antes de "Otros"
    return todas

# Mapeo de sectores de Yahoo Finance a nuestras categorías
SECTOR_MAPPING = {
    # Sectores de Yahoo Finance (acciones)
    'Technology': 'Tecnología',
    'Healthcare': 'Salud',
    'Financial Services': 'Finanzas',
    'Financial': 'Finanzas',
    'Energy': 'Energía',
    'Consumer Cyclical': 'Consumo',
    'Consumer Defensive': 'Consumo',
    'Consumer Goods': 'Consumo',
    'Industrials': 'Industrial',
    'Basic Materials': 'Materiales',
    'Real Estate': 'Inmobiliario',
    'Communication Services': 'Comunicaciones',
    'Utilities': 'Utilities',
    
    # Categorías de ETFs
    'Large Blend': 'Global/Diversificado',
    'Large Growth': 'Tecnología',
    'Large Value': 'Global/Diversificado',
    'Mid-Cap Blend': 'Global/Diversificado',
    'Small Blend': 'Global/Diversificado',
    'Foreign Large Blend': 'Global/Diversificado',
    'Diversified Emerging Mkts': 'Mercados Emergentes',
    'Emerging Markets': 'Mercados Emergentes',
    'Europe Stock': 'Global/Diversificado',
    'World Stock': 'Global/Diversificado',
    'Technology': 'Tecnología',
    'Health': 'Salud',
    'Equity Precious Metals': 'Oro/Materias Primas',
    'Commodities Broad Basket': 'Oro/Materias Primas',
    'Natural Resources': 'Oro/Materias Primas',
    'Corporate Bond': 'Renta Fija',
    'Government Bond': 'Renta Fija',
    'High Yield Bond': 'Renta Fija',
    'Inflation-Protected Bond': 'Renta Fija',
    'Intermediate Core Bond': 'Renta Fija',
    'Intermediate Government': 'Renta Fija',
    'Long Government': 'Renta Fija',
    'Long-Term Bond': 'Renta Fija',
    'Short Government': 'Renta Fija',
    'Short-Term Bond': 'Renta Fija',
    'Ultrashort Bond': 'Renta Fija',
    'World Bond': 'Renta Fija',
}

# Palabras clave en el nombre para detectar categoría
KEYWORD_MAPPING = {
    'Tecnología': ['tech', 'technology', 'software', 'semiconductor', 'cloud', 'digital', 'nasdaq', 'information'],
    'Salud': ['health', 'healthcare', 'medical', 'pharma', 'biotech', 'salud'],
    'Finanzas': ['financial', 'bank', 'finance', 'insurance', 'finanzas'],
    'Energía': ['energy', 'oil', 'gas', 'petrol', 'energía', 'clean energy', 'solar', 'wind'],
    'Consumo': ['consumer', 'retail', 'consumo', 'food', 'beverage'],
    'Industrial': ['industrial', 'aerospace', 'defense', 'manufacturing'],
    'Materiales': ['materials', 'mining', 'steel', 'chemical', 'materiales'],
    'Inmobiliario': ['real estate', 'reit', 'property', 'inmobiliario'],
    'Comunicaciones': ['communication', 'media', 'telecom', 'comunicaciones'],
    'Utilities': ['utilities', 'electric', 'water', 'servicios'],
    'Renta Fija': ['bond', 'treasury', 'fixed income', 'renta fija', 'government', 'corporate bond', 'aggregate'],
    'Mercados Emergentes': ['emerging', 'emergentes', 'em ', 'china', 'india', 'brazil', 'asia'],
    'Global/Diversificado': ['world', 'global', 'msci', 'all-world', 'developed', 'acwi', 's&p 500', 'total market', 'diversified'],
    'Oro/Materias Primas': ['gold', 'oro', 'silver', 'plata', 'commodity', 'commodities', 'precious', 'materias primas'],
    'Crypto': ['bitcoin', 'btc', 'ethereum', 'eth', 'crypto', 'blockchain', 'digital asset', 'cryptocurrency'],
    'Small Caps': ['russell 2000', 'russell2000', 'small cap', 'small-cap', 'smallcap', 'mid cap', 'mid-cap', 'midcap', 'small companies', 'micro cap'],
}


def detectar_categoria(ticker: str = None, nombre: str = None, isin: str = None) -> str:
    """Detecta automáticamente la categoría de un activo"""
    import yfinance as yf
    
    categoria = ''
    
    # Obtener keywords personalizadas
    custom = cargar_categorias_custom()
    custom_keywords = custom.get('keywords', {})
    
    # 1. Intentar obtener sector/categoría de Yahoo Finance
    if ticker:
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            
            # Para acciones: usar 'sector'
            sector = info.get('sector', '')
            if sector and sector in SECTOR_MAPPING:
                return SECTOR_MAPPING[sector]
            
            # Para ETFs/fondos: usar 'category'
            category = info.get('category', '')
            if category:
                # Buscar coincidencia en el mapeo
                for key, value in SECTOR_MAPPING.items():
                    if key.lower() in category.lower():
                        return value
                        
            # Usar quoteType para algunos casos
            quote_type = info.get('quoteType', '')
            if quote_type == 'MUTUALFUND' or quote_type == 'ETF':
                # Analizar el nombre del fondo
                fund_name = info.get('longName', '') or info.get('shortName', '')
                if fund_name:
                    nombre = fund_name
                    
        except Exception as e:
            pass
    
    # 2. Analizar el nombre por palabras clave (incluyendo custom)
    if nombre:
        nombre_lower = nombre.lower()
        
        # Primero buscar en keywords personalizadas
        for cat, keywords in custom_keywords.items():
            for keyword in keywords:
                if keyword.lower() in nombre_lower:
                    return cat
        
        # Luego en las predefinidas
        for cat, keywords in KEYWORD_MAPPING.items():
            for keyword in keywords:
                if keyword.lower() in nombre_lower:
                    return cat
    
    # 3. Si no se detecta, devolver vacío (el usuario puede asignarla)
    return categoria


@app.route('/api/categories/list')
def api_categories_list():
    """Devuelve lista de categorías disponibles (predefinidas + custom)"""
    todas = obtener_todas_categorias()
    return jsonify({'success': True, 'data': todas})


@app.route('/api/categories/custom', methods=['GET'])
def api_get_custom_categories():
    """Obtiene las categorías personalizadas"""
    custom = cargar_categorias_custom()
    return jsonify({'success': True, 'data': custom})


@app.route('/api/categories/custom', methods=['POST'])
def api_add_custom_category():
    """Añade una nueva categoría personalizada"""
    data = request.get_json()
    nombre = data.get('nombre', '').strip()
    keywords = data.get('keywords', [])
    
    if not nombre:
        return jsonify({'success': False, 'error': 'Nombre de categoría requerido'})
    
    # Verificar que no existe
    todas = obtener_todas_categorias()
    if nombre in todas:
        return jsonify({'success': False, 'error': 'La categoría ya existe'})
    
    custom = cargar_categorias_custom()
    
    if nombre not in custom['categories']:
        custom['categories'].append(nombre)
    
    if keywords:
        custom['keywords'][nombre] = [k.lower().strip() for k in keywords if k.strip()]
    
    guardar_categorias_custom(custom)
    
    return jsonify({'success': True, 'message': f'Categoría "{nombre}" creada'})


@app.route('/api/categories/custom/<nombre>', methods=['DELETE'])
def api_delete_custom_category(nombre):
    """Elimina una categoría personalizada"""
    custom = cargar_categorias_custom()
    
    if nombre in custom['categories']:
        custom['categories'].remove(nombre)
    
    if nombre in custom['keywords']:
        del custom['keywords'][nombre]
    
    guardar_categorias_custom(custom)
    
    return jsonify({'success': True, 'message': f'Categoría "{nombre}" eliminada'})


@app.route('/api/categories/custom/<nombre>/keywords', methods=['PUT'])
def api_update_category_keywords(nombre):
    """Actualiza las palabras clave de una categoría"""
    data = request.get_json()
    keywords = data.get('keywords', [])
    
    custom = cargar_categorias_custom()
    
    # Puede ser una categoría predefinida o custom
    if keywords:
        custom['keywords'][nombre] = [k.lower().strip() for k in keywords if k.strip()]
    elif nombre in custom['keywords']:
        del custom['keywords'][nombre]
    
    guardar_categorias_custom(custom)
    
    return jsonify({'success': True, 'message': f'Keywords actualizadas para "{nombre}"'})


@app.route('/api/portfolio/returns')
def api_portfolio_returns():
    """Calcula rentabilidad por diferentes períodos"""
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        import yfinance as yf
        from datetime import datetime, timedelta
        from src.scrapers import JustETFScraper
        
        justetf = JustETFScraper()
        
        # Calcular coste total real (lo que invirtió el usuario)
        coste_total = sum(pos.coste_total for pos in portfolio.posiciones)
        
        # Obtener valor actual
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios()
        valor_actual = sum(p.valor_actual for p in posiciones)
        
        # Rentabilidad total desde inicio
        rent_total = ((valor_actual - coste_total) / coste_total * 100) if coste_total > 0 else 0
        
        # Para calcular rentabilidades por período, necesitamos histórico
        # Obtener histórico de 1 año para calcular todos los períodos
        all_data = {}
        for pos in portfolio.posiciones:
            hist_data = None
            
            if pos.ticker:
                try:
                    ticker = yf.Ticker(pos.ticker)
                    hist = ticker.history(period='1y')
                    if not hist.empty:
                        hist_data = {d.strftime('%Y-%m-%d'): hist.loc[d, 'Close'] for d in hist.index}
                except:
                    pass
            
            if not hist_data and pos.isin:
                try:
                    historico = justetf.obtener_historico(pos.isin, '1y')
                    if historico and historico.get('precios'):
                        hist_data = dict(zip(historico['fechas'], historico['precios']))
                except:
                    pass
            
            if hist_data:
                all_data[pos.id] = {
                    'hist': hist_data,
                    'cantidad': pos.cantidad
                }
        
        # Calcular valor de cartera para fechas específicas
        def calcular_valor_en_fecha(fecha_str):
            valor = 0
            for pos_id, data in all_data.items():
                # Buscar precio más cercano a la fecha
                fechas_ordenadas = sorted(data['hist'].keys())
                precio = None
                for f in fechas_ordenadas:
                    if f <= fecha_str:
                        precio = data['hist'][f]
                    else:
                        break
                if precio:
                    valor += precio * data['cantidad']
            return valor
        
        hoy = datetime.now()
        
        # Calcular rentabilidades
        periodos = {}
        
        # Hoy vs Ayer
        ayer = (hoy - timedelta(days=1)).strftime('%Y-%m-%d')
        valor_ayer = calcular_valor_en_fecha(ayer)
        if valor_ayer > 0:
            periodos['diaria'] = {
                'label': 'Hoy',
                'rentabilidad': round(((valor_actual - valor_ayer) / valor_ayer) * 100, 2),
                'cambio': round(valor_actual - valor_ayer, 2)
            }
        
        # Esta semana (desde el lunes)
        dias_desde_lunes = hoy.weekday()
        inicio_semana = (hoy - timedelta(days=dias_desde_lunes)).strftime('%Y-%m-%d')
        valor_inicio_semana = calcular_valor_en_fecha(inicio_semana)
        if valor_inicio_semana > 0:
            periodos['semanal'] = {
                'label': 'Esta semana',
                'rentabilidad': round(((valor_actual - valor_inicio_semana) / valor_inicio_semana) * 100, 2),
                'cambio': round(valor_actual - valor_inicio_semana, 2)
            }
        
        # Este mes
        inicio_mes = hoy.replace(day=1).strftime('%Y-%m-%d')
        valor_inicio_mes = calcular_valor_en_fecha(inicio_mes)
        if valor_inicio_mes > 0:
            periodos['mensual'] = {
                'label': 'Este mes',
                'rentabilidad': round(((valor_actual - valor_inicio_mes) / valor_inicio_mes) * 100, 2),
                'cambio': round(valor_actual - valor_inicio_mes, 2)
            }
        
        # YTD (Year to Date)
        inicio_ano = hoy.replace(month=1, day=1).strftime('%Y-%m-%d')
        valor_inicio_ano = calcular_valor_en_fecha(inicio_ano)
        if valor_inicio_ano > 0:
            periodos['ytd'] = {
                'label': 'Este año (YTD)',
                'rentabilidad': round(((valor_actual - valor_inicio_ano) / valor_inicio_ano) * 100, 2),
                'cambio': round(valor_actual - valor_inicio_ano, 2)
            }
        
        # Último mes (30 días)
        hace_30_dias = (hoy - timedelta(days=30)).strftime('%Y-%m-%d')
        valor_hace_30 = calcular_valor_en_fecha(hace_30_dias)
        if valor_hace_30 > 0:
            periodos['30d'] = {
                'label': 'Últimos 30 días',
                'rentabilidad': round(((valor_actual - valor_hace_30) / valor_hace_30) * 100, 2),
                'cambio': round(valor_actual - valor_hace_30, 2)
            }
        
        # Últimos 3 meses
        hace_90_dias = (hoy - timedelta(days=90)).strftime('%Y-%m-%d')
        valor_hace_90 = calcular_valor_en_fecha(hace_90_dias)
        if valor_hace_90 > 0:
            periodos['90d'] = {
                'label': 'Últimos 3 meses',
                'rentabilidad': round(((valor_actual - valor_hace_90) / valor_hace_90) * 100, 2),
                'cambio': round(valor_actual - valor_hace_90, 2)
            }
        
        # Desde inicio (rentabilidad real)
        periodos['total'] = {
            'label': 'Desde inicio',
            'rentabilidad': round(rent_total, 2),
            'cambio': round(valor_actual - coste_total, 2)
        }
        
        return jsonify({
            'success': True,
            'data': {
                'periodos': periodos,
                'valor_actual': round(valor_actual, 2),
                'coste_total': round(coste_total, 2)
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/categories/detect', methods=['POST'])
def api_detect_category():
    """Detecta la categoría de un activo automáticamente"""
    data = request.get_json()
    ticker = data.get('ticker', '')
    nombre = data.get('nombre', '')
    isin = data.get('isin', '')
    
    categoria = detectar_categoria(ticker, nombre, isin)
    
    return jsonify({
        'success': True,
        'data': {
            'categoria': categoria,
            'auto_detected': bool(categoria)
        }
    })


# Archivo para guardar los objetivos de asignación
TARGETS_FILE = os.path.join(DATA_DIR, 'targets.json')

def cargar_targets():
    """Carga los objetivos de asignación por categoría"""
    # En BD solo usamos targets por posición
    if USE_DATABASE:
        return {}
    if os.path.exists(TARGETS_FILE):
        with open(TARGETS_FILE, 'r') as f:
            return json.load(f)
    return {}

def cargar_targets_positions():
    """Carga los objetivos de asignación por posición (ISIN)"""
    if USE_DATABASE:
        targets = Target.query.all()
        return {t.isin: t.porcentaje for t in targets}
    
    targets_file = os.path.join(DATA_DIR, 'targets_positions.json')
    if os.path.exists(targets_file):
        with open(targets_file, 'r') as f:
            return json.load(f)
    return {}

def guardar_targets(targets):
    """Guarda los objetivos de asignación por categoría"""
    if not USE_DATABASE:
        with open(TARGETS_FILE, 'w') as f:
            json.dump(targets, f, indent=2)

def guardar_targets_positions(targets):
    """Guarda los objetivos de asignación por posición"""
    if USE_DATABASE:
        Target.query.delete()
        for isin, porcentaje in targets.items():
            target = Target(isin=isin, porcentaje=porcentaje)
            db.session.add(target)
        db.session.commit()
    else:
        targets_file = os.path.join(DATA_DIR, 'targets_positions.json')
        with open(targets_file, 'w') as f:
            json.dump(targets, f, indent=2)

def cargar_nuevos_activos():
    """Carga los activos nuevos planificados"""
    if USE_DATABASE:
        nuevos = ActivoNuevo.query.all()
        return {n.isin: {'nombre': n.nombre, 'categoria': n.categoria, 'precio': n.precio} for n in nuevos}
    
    nuevos_file = os.path.join(DATA_DIR, 'nuevos_activos.json')
    if os.path.exists(nuevos_file):
        with open(nuevos_file, 'r') as f:
            return json.load(f)
    return {}

def guardar_nuevos_activos(nuevos):
    """Guarda los activos nuevos planificados"""
    if USE_DATABASE:
        ActivoNuevo.query.delete()
        for isin, info in nuevos.items():
            nuevo = ActivoNuevo(
                isin=isin,
                nombre=info.get('nombre'),
                categoria=info.get('categoria'),
                precio=info.get('precio')
            )
            db.session.add(nuevo)
        db.session.commit()
    else:
        nuevos_file = os.path.join(DATA_DIR, 'nuevos_activos.json')
        with open(nuevos_file, 'w') as f:
            json.dump(nuevos, f, indent=2)


@app.route('/api/targets', methods=['GET'])
@login_required
def api_get_targets():
    """Obtiene los objetivos de asignación"""
    targets = cargar_targets()
    return jsonify({'success': True, 'data': targets})


@app.route('/api/targets', methods=['POST'])
def api_set_targets():
    """Guarda los objetivos de asignación"""
    data = request.get_json()
    
    # Validar que los porcentajes suman 100 (o menos)
    total = sum(data.values())
    if total > 100:
        return jsonify({'success': False, 'error': f'Los porcentajes suman {total}%, deben ser máximo 100%'})
    
    guardar_targets(data)
    return jsonify({'success': True, 'message': 'Objetivos guardados'})


@app.route('/api/portfolio/allocation')
def api_portfolio_allocation():
    """Compara asignación actual vs objetivos (por posición o categoría)"""
    portfolio = cargar_portfolio()
    
    # Primero intentar cargar targets por posición
    targets_pos = cargar_targets_positions()
    usar_por_posicion = len(targets_pos) > 0
    
    # Si no hay por posición, usar por categoría
    targets_cat = cargar_targets() if not usar_por_posicion else {}
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios()
        
        # Calcular valor total
        valor_total = sum(p.valor_actual for p in posiciones)
        
        allocation = []
        
        if usar_por_posicion:
            # Modo por posición: mostrar cada activo con su target
            for pos in posiciones:
                key = pos.isin or pos.id
                peso_objetivo = targets_pos.get(key, 0)
                
                if peso_objetivo == 0:
                    continue  # Saltar activos sin target
                
                peso_actual = (pos.valor_actual / valor_total * 100) if valor_total > 0 else 0
                diferencia = peso_actual - peso_objetivo
                
                valor_objetivo = (peso_objetivo / 100) * valor_total
                ajuste = valor_objetivo - pos.valor_actual
                
                if abs(diferencia) <= 2:
                    estado = 'ok'
                elif diferencia > 0:
                    estado = 'sobreponderado'
                else:
                    estado = 'infraponderado'
                
                allocation.append({
                    'categoria': pos.nombre[:30] + '...' if len(pos.nombre) > 30 else pos.nombre,
                    'valor': round(pos.valor_actual, 2),
                    'peso_actual': round(peso_actual, 2),
                    'peso_objetivo': peso_objetivo,
                    'diferencia': round(diferencia, 2),
                    'ajuste': round(ajuste, 2),
                    'estado': estado,
                    'isin': pos.isin
                })
        else:
            # Modo por categoría (legacy)
            por_categoria = {}
            for pos in posiciones:
                cat = pos.categoria if pos.categoria else 'Sin categoría'
                if cat not in por_categoria:
                    por_categoria[cat] = {'valor': 0, 'posiciones': []}
                por_categoria[cat]['valor'] += pos.valor_actual
                por_categoria[cat]['posiciones'].append({
                    'nombre': pos.nombre,
                    'valor': pos.valor_actual
                })
            
            for cat in set(list(por_categoria.keys()) + list(targets_cat.keys())):
                valor = por_categoria.get(cat, {}).get('valor', 0)
                peso_actual = (valor / valor_total * 100) if valor_total > 0 else 0
                peso_objetivo = targets_cat.get(cat, 0)
                diferencia = peso_actual - peso_objetivo
                
                valor_objetivo = (peso_objetivo / 100) * valor_total
                ajuste = valor_objetivo - valor
                
                if peso_objetivo == 0:
                    estado = 'sin_objetivo'
                elif abs(diferencia) <= 2:
                    estado = 'ok'
                elif diferencia > 0:
                    estado = 'sobreponderado'
                else:
                    estado = 'infraponderado'
                
                allocation.append({
                    'categoria': cat,
                    'valor': round(valor, 2),
                    'peso_actual': round(peso_actual, 2),
                    'peso_objetivo': peso_objetivo,
                    'diferencia': round(diferencia, 2),
                    'ajuste': round(ajuste, 2),
                    'estado': estado,
                    'posiciones': por_categoria.get(cat, {}).get('posiciones', [])
                })
        
        # Ordenar por diferencia (más desviados primero)
        allocation.sort(key=lambda x: abs(x['diferencia']), reverse=True)
        
        tiene_objetivos = len(targets_pos) > 0 or len(targets_cat) > 0
        
        return jsonify({
            'success': True,
            'data': {
                'allocation': allocation,
                'valor_total': round(valor_total, 2),
                'tiene_objetivos': tiene_objetivos,
                'modo': 'posicion' if usar_por_posicion else 'categoria'
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/positions/weights')
def api_positions_weights():
    """Devuelve las posiciones con su peso actual en la cartera"""
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': True, 'data': {'positions': [], 'valor_total': 0}})
    
    try:
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios()
        
        valor_total = sum(p.valor_actual for p in posiciones)
        
        positions = []
        for pos in posiciones:
            peso = (pos.valor_actual / valor_total * 100) if valor_total > 0 else 0
            positions.append({
                'id': pos.id,
                'isin': pos.isin,
                'ticker': pos.ticker,
                'nombre': pos.nombre,
                'categoria': pos.categoria or 'Sin categoría',
                'valor': round(pos.valor_actual, 2),
                'peso_actual': round(peso, 2)
            })
        
        # Ordenar por peso (mayor a menor)
        positions.sort(key=lambda x: x['peso_actual'], reverse=True)
        
        return jsonify({
            'success': True,
            'data': {
                'positions': positions,
                'valor_total': round(valor_total, 2)
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/targets/positions', methods=['GET', 'POST'])
@login_required
def api_targets_positions():
    """Gestiona los targets por posición individual (ISIN) incluyendo activos nuevos"""
    
    if request.method == 'GET':
        try:
            targets = cargar_targets_positions()
            nuevos = cargar_nuevos_activos()
            return jsonify({'success': True, 'data': targets, 'nuevosActivos': nuevos})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
    
    else:  # POST
        try:
            data = request.get_json()
            
            # Soportar tanto formato antiguo (solo targets) como nuevo (targets + nuevosActivos)
            if isinstance(data, dict) and 'targets' in data:
                targets = data.get('targets', {})
                nuevos = data.get('nuevosActivos', {})
            else:
                # Formato antiguo: solo targets
                targets = data
                nuevos = {}
            
            # Guardar targets
            guardar_targets_positions(targets)
            
            # Guardar nuevos activos si existen
            if nuevos:
                nuevos_actuales = cargar_nuevos_activos()
                nuevos_actuales.update(nuevos)
                # Limpiar activos que ya no tienen target
                nuevos_actuales = {k: v for k, v in nuevos_actuales.items() if k in targets}
                guardar_nuevos_activos(nuevos_actuales)
            
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/metrics')
def api_portfolio_metrics():
    """Calcula métricas avanzadas: Volatilidad, Max Drawdown, Sharpe Ratio"""
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        import yfinance as yf
        import numpy as np
        from datetime import datetime, timedelta
        from src.scrapers import JustETFScraper
        
        justetf = JustETFScraper()
        
        # Obtener histórico de 1 año para calcular métricas
        all_data = {}
        for pos in portfolio.posiciones:
            hist_data = None
            
            if pos.ticker:
                try:
                    ticker = yf.Ticker(pos.ticker)
                    hist = ticker.history(period='1y')
                    if not hist.empty:
                        hist_data = {d.strftime('%Y-%m-%d'): hist.loc[d, 'Close'] for d in hist.index}
                except:
                    pass
            
            if not hist_data and pos.isin:
                try:
                    historico = justetf.obtener_historico(pos.isin, '1y')
                    if historico and historico.get('precios'):
                        hist_data = dict(zip(historico['fechas'], historico['precios']))
                except:
                    pass
            
            if hist_data:
                all_data[pos.id] = {
                    'hist': hist_data,
                    'cantidad': pos.cantidad
                }
        
        if not all_data:
            return jsonify({'success': False, 'error': 'No hay datos históricos'})
        
        # Calcular valor diario de la cartera
        all_fechas = set()
        for data in all_data.values():
            all_fechas.update(data['hist'].keys())
        
        fechas_ordenadas = sorted(list(all_fechas))
        valores_diarios = []
        
        for fecha in fechas_ordenadas:
            valor_total = 0
            posiciones_con_valor = 0
            for pos_id, data in all_data.items():
                if fecha in data['hist']:
                    valor_total += data['hist'][fecha] * data['cantidad']
                    posiciones_con_valor += 1
            
            if posiciones_con_valor >= len(all_data) / 2:
                valores_diarios.append(valor_total)
        
        if len(valores_diarios) < 20:
            return jsonify({'success': False, 'error': 'Datos insuficientes para calcular métricas'})
        
        valores = np.array(valores_diarios)
        
        # Calcular retornos diarios
        retornos = np.diff(valores) / valores[:-1]
        
        # 1. VOLATILIDAD (desviación estándar anualizada)
        volatilidad_diaria = np.std(retornos)
        volatilidad_anual = volatilidad_diaria * np.sqrt(252)  # 252 días de trading
        
        # 2. MAX DRAWDOWN
        peak = valores[0]
        max_drawdown = 0
        for valor in valores:
            if valor > peak:
                peak = valor
            drawdown = (peak - valor) / peak
            if drawdown > max_drawdown:
                max_drawdown = drawdown
        
        # 3. SHARPE RATIO (asumiendo tasa libre de riesgo del 3% anual)
        risk_free_rate = 0.03
        retorno_medio_diario = np.mean(retornos)
        retorno_anual = retorno_medio_diario * 252
        
        if volatilidad_anual > 0:
            sharpe_ratio = (retorno_anual - risk_free_rate) / volatilidad_anual
        else:
            sharpe_ratio = 0
        
        # 4. Retorno total
        retorno_total = (valores[-1] - valores[0]) / valores[0]
        
        # 5. Mejor y peor día
        mejor_dia = np.max(retornos) * 100
        peor_dia = np.min(retornos) * 100
        
        # 6. Días positivos vs negativos
        dias_positivos = np.sum(retornos > 0)
        dias_negativos = np.sum(retornos < 0)
        
        return jsonify({
            'success': True,
            'data': {
                'volatilidad': {
                    'valor': round(volatilidad_anual * 100, 2),
                    'descripcion': 'Volatilidad anualizada',
                    'interpretacion': 'Baja' if volatilidad_anual < 0.15 else 'Media' if volatilidad_anual < 0.25 else 'Alta'
                },
                'max_drawdown': {
                    'valor': round(max_drawdown * 100, 2),
                    'descripcion': 'Máxima caída desde máximo',
                    'interpretacion': 'Bajo' if max_drawdown < 0.10 else 'Moderado' if max_drawdown < 0.20 else 'Alto'
                },
                'sharpe_ratio': {
                    'valor': round(sharpe_ratio, 2),
                    'descripcion': 'Retorno ajustado por riesgo',
                    'interpretacion': 'Excelente' if sharpe_ratio > 1 else 'Bueno' if sharpe_ratio > 0.5 else 'Bajo' if sharpe_ratio > 0 else 'Negativo'
                },
                'retorno_total': {
                    'valor': round(retorno_total * 100, 2),
                    'descripcion': 'Retorno en el período'
                },
                'mejor_dia': round(mejor_dia, 2),
                'peor_dia': round(peor_dia, 2),
                'dias_positivos': int(dias_positivos),
                'dias_negativos': int(dias_negativos),
                'ratio_dias': round(dias_positivos / (dias_positivos + dias_negativos) * 100, 1) if (dias_positivos + dias_negativos) > 0 else 0
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


# Datos de exposición geográfica por tipo de ETF/Fondo
EXPOSICION_GEOGRAFICA = {
    # Por palabras clave en el nombre
    'global': {'USA': 60, 'Europa': 15, 'Japón': 6, 'UK': 4, 'Otros': 15},
    'world': {'USA': 60, 'Europa': 15, 'Japón': 6, 'UK': 4, 'Otros': 15},
    'msci world': {'USA': 68, 'Europa': 12, 'Japón': 6, 'UK': 4, 'Otros': 10},
    'msci acwi': {'USA': 58, 'Europa': 12, 'Emergentes': 12, 'Japón': 5, 'Otros': 13},
    's&p 500': {'USA': 100},
    'sp500': {'USA': 100},
    'nasdaq': {'USA': 100},
    'russell': {'USA': 100},
    'euro stoxx': {'Europa': 100},
    'stoxx 600': {'Europa': 100},
    'stoxx europe': {'Europa': 100},
    'europe': {'Europa': 100},
    'emerging': {'China': 30, 'Taiwán': 15, 'India': 12, 'Corea': 12, 'Brasil': 8, 'Otros EM': 23},
    'emergentes': {'China': 30, 'Taiwán': 15, 'India': 12, 'Corea': 12, 'Brasil': 8, 'Otros EM': 23},
    'china': {'China': 100},
    'india': {'India': 100},
    'japan': {'Japón': 100},
    'japon': {'Japón': 100},
    'uk': {'UK': 100},
    'ftse 100': {'UK': 100},
    'dax': {'Alemania': 100},
    'ibex': {'España': 100},
    'spain': {'España': 100},
    'gold': {'Global': 100},
    'oro': {'Global': 100},
    'silver': {'Global': 100},
    'plata': {'Global': 100},
    'bitcoin': {'Global': 100},
    'crypto': {'Global': 100},
    'bond': {'Según emisor': 100},
    'treasury': {'USA': 100},
    'aggregate': {'USA': 40, 'Europa': 30, 'Otros': 30},
}


@app.route('/api/portfolio/geography')
def api_portfolio_geography():
    """Calcula la exposición geográfica de la cartera"""
    portfolio = cargar_portfolio()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    try:
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios()
        
        valor_total = sum(p.valor_actual for p in posiciones)
        
        # Calcular exposición por país
        exposicion = {}
        detalles = []
        
        for pos in posiciones:
            nombre_lower = pos.nombre.lower()
            peso_posicion = pos.valor_actual / valor_total if valor_total > 0 else 0
            
            # Encontrar la exposición geográfica de este activo
            geo_activo = None
            for keyword, geo in EXPOSICION_GEOGRAFICA.items():
                if keyword in nombre_lower:
                    geo_activo = geo
                    break
            
            # Si no encontramos, asumir Global
            if not geo_activo:
                geo_activo = {'Global/Otros': 100}
            
            # Añadir a la exposición total
            for pais, pct in geo_activo.items():
                contribucion = peso_posicion * (pct / 100)
                if pais not in exposicion:
                    exposicion[pais] = 0
                exposicion[pais] += contribucion
            
            detalles.append({
                'nombre': pos.nombre,
                'valor': pos.valor_actual,
                'peso': round(peso_posicion * 100, 2),
                'geografia': geo_activo
            })
        
        # Convertir a porcentajes y ordenar
        exposicion_list = []
        for pais, valor in exposicion.items():
            exposicion_list.append({
                'pais': pais,
                'porcentaje': round(valor * 100, 2),
                'valor': round(valor * valor_total, 2)
            })
        
        exposicion_list.sort(key=lambda x: x['porcentaje'], reverse=True)
        
        return jsonify({
            'success': True,
            'data': {
                'exposicion': exposicion_list,
                'detalles': detalles,
                'valor_total': round(valor_total, 2)
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/rebalance')
def api_portfolio_rebalance():
    """Genera sugerencias de rebalanceo basadas en los objetivos"""
    portfolio = cargar_portfolio()
    targets = cargar_targets()
    
    if not portfolio.posiciones:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    if not targets:
        return jsonify({'success': False, 'error': 'No hay objetivos configurados'})
    
    try:
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios()
        
        valor_total = sum(p.valor_actual for p in posiciones)
        
        # Agrupar por categoría
        por_categoria = {}
        for pos in posiciones:
            cat = pos.categoria if pos.categoria else 'Sin categoría'
            if cat not in por_categoria:
                por_categoria[cat] = {
                    'valor': 0,
                    'posiciones': []
                }
            por_categoria[cat]['valor'] += pos.valor_actual
            por_categoria[cat]['posiciones'].append({
                'id': pos.id,
                'nombre': pos.nombre,
                'valor': pos.valor_actual,
                'cantidad': pos.cantidad,
                'precio': pos.precio_actual
            })
        
        # Calcular ajustes necesarios
        sugerencias = []
        resumen = {
            'comprar': [],
            'vender': [],
            'ok': []
        }
        
        for cat, target_pct in targets.items():
            valor_actual = por_categoria.get(cat, {}).get('valor', 0)
            valor_objetivo = (target_pct / 100) * valor_total
            diferencia = valor_objetivo - valor_actual
            pct_actual = (valor_actual / valor_total * 100) if valor_total > 0 else 0
            
            if abs(diferencia) < 50:  # Tolerancia de 50€
                resumen['ok'].append({
                    'categoria': cat,
                    'mensaje': f'{cat} está equilibrado'
                })
                continue
            
            if diferencia > 0:
                # Necesita comprar
                posiciones_cat = por_categoria.get(cat, {}).get('posiciones', [])
                
                if posiciones_cat:
                    # Sugerir comprar más del activo existente
                    pos = posiciones_cat[0]  # El primero de la categoría
                    unidades = diferencia / pos['precio'] if pos['precio'] > 0 else 0
                    
                    sugerencia = {
                        'tipo': 'comprar',
                        'categoria': cat,
                        'activo': pos['nombre'],
                        'cantidad': round(unidades, 4),
                        'importe': round(diferencia, 2),
                        'razon': f'Infraponderado: {pct_actual:.1f}% vs objetivo {target_pct}%',
                        'icono': '🟢'
                    }
                else:
                    sugerencia = {
                        'tipo': 'comprar',
                        'categoria': cat,
                        'activo': f'Nuevo activo de {cat}',
                        'cantidad': None,
                        'importe': round(diferencia, 2),
                        'razon': f'No tienes exposición a {cat} (objetivo: {target_pct}%)',
                        'icono': '🟢'
                    }
                
                sugerencias.append(sugerencia)
                resumen['comprar'].append(sugerencia)
            
            else:
                # Necesita vender (sobreponderado)
                posiciones_cat = por_categoria.get(cat, {}).get('posiciones', [])
                
                if posiciones_cat:
                    pos = posiciones_cat[0]
                    unidades = abs(diferencia) / pos['precio'] if pos['precio'] > 0 else 0
                    
                    sugerencia = {
                        'tipo': 'vender',
                        'categoria': cat,
                        'activo': pos['nombre'],
                        'cantidad': round(unidades, 4),
                        'importe': round(abs(diferencia), 2),
                        'razon': f'Sobreponderado: {pct_actual:.1f}% vs objetivo {target_pct}%',
                        'icono': '🔴'
                    }
                    
                    sugerencias.append(sugerencia)
                    resumen['vender'].append(sugerencia)
        
        # Ordenar por importe (mayor primero)
        sugerencias.sort(key=lambda x: x['importe'], reverse=True)
        
        # Calcular aportación sugerida (opcional)
        total_comprar = sum(s['importe'] for s in resumen['comprar'])
        total_vender = sum(s['importe'] for s in resumen['vender'])
        
        return jsonify({
            'success': True,
            'data': {
                'sugerencias': sugerencias,
                'resumen': {
                    'total_comprar': round(total_comprar, 2),
                    'total_vender': round(total_vender, 2),
                    'neto': round(total_comprar - total_vender, 2),
                    'categorias_ok': len(resumen['ok']),
                    'categorias_ajustar': len(resumen['comprar']) + len(resumen['vender'])
                },
                'valor_cartera': round(valor_total, 2)
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/rebalance/calculate', methods=['POST'])
def api_rebalance_calculate():
    """Calcula exactamente qué comprar/vender para rebalancear por activo"""
    data = request.get_json()
    aportacion = float(data.get('aportacion', 0))
    solo_compras = data.get('solo_compras', True)  # False = modo puro con ventas
    
    portfolio = cargar_portfolio()
    
    # Intentar cargar targets por posición primero, si no hay, usar por categoría
    targets = cargar_targets_positions()
    usar_por_posicion = len(targets) > 0
    
    # Cargar activos nuevos (que no están en cartera pero tienen target)
    nuevos_file = os.path.join(DATA_DIR, 'nuevos_activos.json')
    nuevos_activos = {}
    if os.path.exists(nuevos_file):
        with open(nuevos_file, 'r') as f:
            nuevos_activos = json.load(f)
    
    if not usar_por_posicion:
        targets = cargar_targets()
    
    if not portfolio.posiciones and not nuevos_activos:
        return jsonify({'success': False, 'error': 'No hay posiciones'})
    
    if not targets:
        return jsonify({'success': False, 'error': 'Configura primero tus objetivos en Target Allocation'})
    
    try:
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios() if portfolio.posiciones else []
        
        valor_actual = sum(p.valor_actual for p in posiciones)
        valor_futuro = valor_actual + aportacion
        
        # ISINs de posiciones actuales
        isins_en_cartera = set(p.isin or p.id for p in posiciones)
        
        # Calcular distribución por activo (usando ISIN como clave)
        distribucion = []
        compras = []
        ventas = []
        total_a_comprar = 0
        total_a_vender = 0
        
        # Procesar posiciones existentes
        for pos in posiciones:
            # Determinar el target para esta posición
            if usar_por_posicion:
                key = pos.isin or pos.id
                target_pct = targets.get(key, 0)
            else:
                # Usar target por categoría
                target_pct = targets.get(pos.categoria, 0)
            
            if target_pct == 0:
                continue  # Saltar activos sin target configurado
            
            pct_actual = (pos.valor_actual / valor_actual * 100) if valor_actual > 0 else 0
            
            # Valor objetivo después de la aportación
            valor_objetivo = (target_pct / 100) * valor_futuro
            diferencia = valor_objetivo - pos.valor_actual
            
            dist_item = {
                'activo': pos.nombre,
                'isin': pos.isin,
                'categoria': pos.categoria,
                'pct_actual': round(pct_actual, 2),
                'pct_objetivo': target_pct,
                'valor_actual': round(pos.valor_actual, 2),
                'valor_objetivo': round(valor_objetivo, 2),
                'diferencia': round(diferencia, 2)
            }
            distribucion.append(dist_item)
            
            # Si necesita comprar (infraponderado)
            if diferencia > 0:
                compra = {
                    'categoria': pos.categoria or 'Sin categoría',
                    'nombre': pos.nombre,
                    'isin': pos.isin,
                    'ticker': pos.ticker,
                    'precio_actual': pos.precio_actual,
                    'importe_ideal': round(diferencia, 2),
                    'unidades_ideal': round(diferencia / pos.precio_actual, 4) if pos.precio_actual > 0 else 0,
                    'prioridad': diferencia
                }
                compras.append(compra)
                total_a_comprar += diferencia
            
            # Si necesita vender (sobreponderado) - solo en modo puro
            elif diferencia < 0 and not solo_compras:
                importe_venta = abs(diferencia)
                unidades_venta = importe_venta / pos.precio_actual if pos.precio_actual > 0 else 0
                # No vender más de lo que tenemos
                unidades_venta = min(unidades_venta, pos.cantidad)
                importe_venta = unidades_venta * pos.precio_actual
                
                if unidades_venta > 0:
                    venta = {
                        'categoria': pos.categoria or 'Sin categoría',
                        'nombre': pos.nombre,
                        'isin': pos.isin,
                        'ticker': pos.ticker,
                        'precio_actual': pos.precio_actual,
                        'importe_ideal': round(importe_venta, 2),
                        'unidades_ideal': round(unidades_venta, 4),
                        'prioridad': importe_venta
                    }
                    ventas.append(venta)
                    total_a_vender += importe_venta
        
        # Procesar activos nuevos (que no están en cartera pero tienen target)
        if usar_por_posicion:
            for isin, target_pct in targets.items():
                if isin in isins_en_cartera:
                    continue  # Ya procesado arriba
                
                if isin not in nuevos_activos:
                    continue  # No tenemos info de este activo
                
                activo_info = nuevos_activos[isin]
                precio = activo_info.get('precio', 0)
                
                if precio <= 0:
                    continue  # No podemos calcular sin precio
                
                # Valor objetivo (el activo no existe, así que valor actual = 0)
                valor_objetivo = (target_pct / 100) * valor_futuro
                diferencia = valor_objetivo  # Todo es compra
                
                dist_item = {
                    'activo': activo_info.get('nombre', isin),
                    'isin': isin,
                    'categoria': activo_info.get('categoria', 'Otros'),
                    'pct_actual': 0,
                    'pct_objetivo': target_pct,
                    'valor_actual': 0,
                    'valor_objetivo': round(valor_objetivo, 2),
                    'diferencia': round(diferencia, 2),
                    'es_nuevo': True
                }
                distribucion.append(dist_item)
                
                # Este activo siempre necesita compra (no está en cartera)
                compra = {
                    'categoria': activo_info.get('categoria', 'Otros'),
                    'nombre': activo_info.get('nombre', isin),
                    'isin': isin,
                    'ticker': None,
                    'precio_actual': precio,
                    'importe_ideal': round(diferencia, 2),
                    'unidades_ideal': round(diferencia / precio, 4),
                    'prioridad': diferencia,
                    'es_nuevo': True
                }
                compras.append(compra)
                total_a_comprar += diferencia
        
        # En modo puro, las ventas financian las compras
        if not solo_compras:
            dinero_disponible = total_a_vender
        else:
            dinero_disponible = aportacion
        
        # Distribuir compras según el dinero disponible
        if dinero_disponible > 0 and total_a_comprar > 0:
            factor = min(1, dinero_disponible / total_a_comprar)
            
            for compra in compras:
                compra['importe_asignado'] = round(compra['importe_ideal'] * factor, 2)
                compra['unidades_comprar'] = round(compra['importe_asignado'] / compra['precio_actual'], 4) if compra['precio_actual'] > 0 else 0
                if compra['precio_actual'] > 10:
                    compra['unidades_redondeadas'] = max(1, round(compra['unidades_comprar']))
                    compra['importe_redondeado'] = round(compra['unidades_redondeadas'] * compra['precio_actual'], 2)
                else:
                    compra['unidades_redondeadas'] = round(compra['unidades_comprar'], 2)
                    compra['importe_redondeado'] = round(compra['unidades_redondeadas'] * compra['precio_actual'], 2)
        
        # Redondear ventas
        for venta in ventas:
            if venta['precio_actual'] > 10:
                venta['unidades_redondeadas'] = max(1, round(venta['unidades_ideal']))
            else:
                venta['unidades_redondeadas'] = round(venta['unidades_ideal'], 2)
            venta['importe_redondeado'] = round(venta['unidades_redondeadas'] * venta['precio_actual'], 2)
        
        # Ordenar por prioridad
        compras.sort(key=lambda x: x.get('prioridad', 0), reverse=True)
        ventas.sort(key=lambda x: x.get('prioridad', 0), reverse=True)
        
        # Calcular totales
        total_compras_redondeado = sum(c.get('importe_redondeado', 0) for c in compras)
        total_ventas_redondeado = sum(v.get('importe_redondeado', 0) for v in ventas)
        
        if solo_compras:
            sobrante = round(aportacion - total_compras_redondeado, 2)
        else:
            sobrante = round(total_ventas_redondeado - total_compras_redondeado, 2)
        
        # Calcular nueva distribución después de operaciones
        nueva_distribucion = []
        for dist in distribucion:
            compra_item = next((c for c in compras if c['isin'] == dist['isin']), None)
            venta_item = next((v for v in ventas if v['isin'] == dist['isin']), None)
            
            importe_compra = compra_item.get('importe_redondeado', 0) if compra_item else 0
            importe_venta = venta_item.get('importe_redondeado', 0) if venta_item else 0
            
            nuevo_valor = dist['valor_actual'] + importe_compra - importe_venta
            nuevo_pct = (nuevo_valor / valor_futuro * 100) if valor_futuro > 0 else 0
            
            nueva_distribucion.append({
                'categoria': dist.get('activo', dist.get('categoria', '')),  # Usar nombre del activo
                'pct_antes': dist['pct_actual'],
                'pct_despues': round(nuevo_pct, 2),
                'pct_objetivo': dist['pct_objetivo'],
                'mejora': round(abs(nuevo_pct - dist['pct_objetivo']) - abs(dist['pct_actual'] - dist['pct_objetivo']), 2)
            })
        
        return jsonify({
            'success': True,
            'data': {
                'aportacion': aportacion,
                'valor_actual': round(valor_actual, 2),
                'valor_futuro': round(valor_futuro, 2),
                'compras': compras,
                'ventas': ventas,
                'total_a_comprar': round(total_compras_redondeado, 2),
                'total_a_vender': round(total_ventas_redondeado, 2),
                'total_a_invertir': round(total_compras_redondeado, 2),
                'sobrante': max(0, sobrante),
                'distribucion_actual': distribucion,
                'nueva_distribucion': nueva_distribucion
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/portfolio/desviaciones')
def api_portfolio_desviaciones():
    """Verifica si hay categorías desviadas del objetivo"""
    portfolio = cargar_portfolio()
    targets = cargar_targets()
    
    if not portfolio.posiciones or not targets:
        return jsonify({'success': True, 'data': {'alertas': []}})
    
    try:
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios()
        
        valor_total = sum(p.valor_actual for p in posiciones)
        if valor_total <= 0:
            return jsonify({'success': True, 'data': {'alertas': []}})
        
        # Calcular distribución por categoría
        por_categoria = {}
        for pos in posiciones:
            cat = pos.categoria if pos.categoria else 'Sin categoría'
            por_categoria[cat] = por_categoria.get(cat, 0) + pos.valor_actual
        
        alertas = []
        umbral_alerta = 5  # Alertar si desviación > 5%
        
        for cat, target_pct in targets.items():
            valor_cat = por_categoria.get(cat, 0)
            pct_actual = (valor_cat / valor_total * 100)
            diferencia = pct_actual - target_pct
            
            if abs(diferencia) >= umbral_alerta:
                alertas.append({
                    'categoria': cat,
                    'actual': round(pct_actual, 2),
                    'objetivo': target_pct,
                    'diferencia': round(diferencia, 2),
                    'tipo': 'sobreponderado' if diferencia > 0 else 'infraponderado'
                })
        
        # Ordenar por desviación absoluta
        alertas.sort(key=lambda x: abs(x['diferencia']), reverse=True)
        
        return jsonify({'success': True, 'data': {'alertas': alertas}})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =====================
# SIMULADOR WHAT IF
# =====================
@app.route('/api/simulator/projection', methods=['POST'])
def api_simulator_projection():
    """Simula proyección de inversión con aportaciones periódicas"""
    data = request.get_json()
    
    capital_inicial = float(data.get('capital_inicial', 0))
    aportacion_mensual = float(data.get('aportacion_mensual', 0))
    anos = int(data.get('anos', 10))
    rentabilidad_anual = float(data.get('rentabilidad_anual', 7))  # % anual esperado
    
    try:
        # Convertir rentabilidad anual a mensual
        rent_mensual = (1 + rentabilidad_anual / 100) ** (1/12) - 1
        
        meses = anos * 12
        
        # Simular mes a mes
        proyeccion = []
        valor = capital_inicial
        total_aportado = capital_inicial
        
        for mes in range(meses + 1):
            ano = mes // 12
            mes_del_ano = mes % 12
            
            proyeccion.append({
                'mes': mes,
                'ano': ano,
                'valor': round(valor, 2),
                'aportado': round(total_aportado, 2),
                'beneficio': round(valor - total_aportado, 2),
                'label': f'Año {ano}' if mes_del_ano == 0 else None
            })
            
            # Aplicar rentabilidad y añadir aportación
            if mes < meses:
                valor = valor * (1 + rent_mensual) + aportacion_mensual
                total_aportado += aportacion_mensual
        
        # Calcular escenarios: pesimista, esperado, optimista
        def calcular_escenario(rent_anual):
            r_mes = (1 + rent_anual / 100) ** (1/12) - 1
            v = capital_inicial
            for m in range(meses):
                v = v * (1 + r_mes) + aportacion_mensual
            return round(v, 2)
        
        escenarios = {
            'pesimista': {
                'rentabilidad': max(0, rentabilidad_anual - 4),
                'valor_final': calcular_escenario(max(0, rentabilidad_anual - 4))
            },
            'esperado': {
                'rentabilidad': rentabilidad_anual,
                'valor_final': calcular_escenario(rentabilidad_anual)
            },
            'optimista': {
                'rentabilidad': rentabilidad_anual + 4,
                'valor_final': calcular_escenario(rentabilidad_anual + 4)
            }
        }
        
        # Resumen
        valor_final = proyeccion[-1]['valor']
        total_aportado_final = proyeccion[-1]['aportado']
        beneficio_total = valor_final - total_aportado_final
        rentabilidad_total = (beneficio_total / total_aportado_final * 100) if total_aportado_final > 0 else 0
        
        return jsonify({
            'success': True,
            'data': {
                'proyeccion': proyeccion,
                'resumen': {
                    'valor_final': round(valor_final, 2),
                    'total_aportado': round(total_aportado_final, 2),
                    'beneficio': round(beneficio_total, 2),
                    'rentabilidad_total': round(rentabilidad_total, 2),
                    'anos': anos,
                    'aportacion_mensual': aportacion_mensual,
                    'rentabilidad_anual': rentabilidad_anual
                },
                'escenarios': escenarios
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =====================
# OBJETIVOS DE AHORRO
# =====================
GOALS_FILE = os.path.join(DATA_DIR, 'goals.json')

def cargar_goals():
    """Carga los objetivos de ahorro"""
    if os.path.exists(GOALS_FILE):
        with open(GOALS_FILE, 'r') as f:
            return json.load(f)
    return []

def guardar_goals(goals):
    """Guarda los objetivos de ahorro"""
    with open(GOALS_FILE, 'w') as f:
        json.dump(goals, f, indent=2)


@app.route('/api/goals', methods=['GET'])
def api_get_goals():
    """Obtiene todos los objetivos de ahorro"""
    goals = cargar_goals()
    portfolio = cargar_portfolio()
    
    # Calcular valor actual de la cartera
    valor_actual = 0
    if portfolio.posiciones:
        try:
            analyzer = PortfolioAnalyzer(portfolio)
            posiciones = analyzer.actualizar_precios()
            valor_actual = sum(p.valor_actual for p in posiciones)
        except:
            valor_actual = sum(p.valor_actual for p in portfolio.posiciones)
    
    # Actualizar progreso de cada objetivo
    from datetime import datetime
    hoy = datetime.now()
    
    for goal in goals:
        goal['valor_actual'] = round(valor_actual, 2)
        goal['progreso'] = round((valor_actual / goal['objetivo']) * 100, 1) if goal['objetivo'] > 0 else 0
        goal['restante'] = round(max(0, goal['objetivo'] - valor_actual), 2)
        
        # Calcular tiempo restante
        if goal.get('fecha_objetivo'):
            fecha_obj = datetime.strptime(goal['fecha_objetivo'], '%Y-%m-%d')
            dias_restantes = (fecha_obj - hoy).days
            goal['dias_restantes'] = max(0, dias_restantes)
            goal['meses_restantes'] = max(0, dias_restantes // 30)
            
            # Calcular ahorro mensual necesario
            if dias_restantes > 0 and goal['restante'] > 0:
                meses = dias_restantes / 30
                goal['ahorro_mensual_necesario'] = round(goal['restante'] / meses, 2) if meses > 0 else 0
            else:
                goal['ahorro_mensual_necesario'] = 0
            
            # Estado del objetivo
            if valor_actual >= goal['objetivo']:
                goal['estado'] = 'completado'
            elif dias_restantes <= 0:
                goal['estado'] = 'vencido'
            elif goal['progreso'] >= (100 - (dias_restantes / 365 * 100)):
                goal['estado'] = 'en_camino'
            else:
                goal['estado'] = 'retrasado'
        else:
            goal['dias_restantes'] = None
            goal['meses_restantes'] = None
            goal['ahorro_mensual_necesario'] = None
            goal['estado'] = 'completado' if valor_actual >= goal['objetivo'] else 'en_progreso'
    
    return jsonify({'success': True, 'data': goals})


@app.route('/api/goals', methods=['POST'])
def api_add_goal():
    """Añade un nuevo objetivo de ahorro"""
    import uuid
    data = request.get_json()
    
    goals = cargar_goals()
    
    new_goal = {
        'id': str(uuid.uuid4())[:8],  # UUID corto para evitar duplicados
        'nombre': data.get('nombre', 'Mi objetivo'),
        'objetivo': float(data.get('objetivo', 0)),
        'fecha_objetivo': data.get('fecha_objetivo'),  # YYYY-MM-DD o None
        'icono': data.get('icono', '🎯'),
        'color': data.get('color', '#3b82f6'),
        'fecha_creacion': datetime.now().strftime('%Y-%m-%d')
    }
    
    goals.append(new_goal)
    guardar_goals(goals)
    
    return jsonify({'success': True, 'data': new_goal})


@app.route('/api/goals/<goal_id>', methods=['DELETE'])
def api_delete_goal(goal_id):
    """Elimina un objetivo"""
    goals = cargar_goals()
    goals = [g for g in goals if g['id'] != goal_id]
    guardar_goals(goals)
    
    return jsonify({'success': True})


@app.route('/api/goals/<goal_id>', methods=['PUT'])
def api_update_goal(goal_id):
    """Actualiza un objetivo"""
    data = request.get_json()
    goals = cargar_goals()
    
    for goal in goals:
        if goal['id'] == goal_id:
            goal['nombre'] = data.get('nombre', goal['nombre'])
            goal['objetivo'] = float(data.get('objetivo', goal['objetivo']))
            goal['fecha_objetivo'] = data.get('fecha_objetivo', goal.get('fecha_objetivo'))
            goal['icono'] = data.get('icono', goal.get('icono', '🎯'))
            break
    
    guardar_goals(goals)
    return jsonify({'success': True})


@app.route('/api/categories/auto-assign', methods=['POST'])
def api_auto_assign_categories():
    """Auto-asigna categorías a todas las posiciones sin categoría"""
    try:
        portfolio = cargar_portfolio()
        asignadas = 0
        
        for pos in portfolio.posiciones:
            if not pos.categoria:
                categoria = detectar_categoria(pos.ticker, pos.nombre, pos.isin)
                if categoria:
                    pos.categoria = categoria
                    asignadas += 1
        
        if asignadas > 0:
            guardar_portfolio(portfolio)
        
        return jsonify({
            'success': True,
            'message': f'Se asignaron {asignadas} categorías automáticamente',
            'asignadas': asignadas
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/position/delete/<position_id>', methods=['DELETE'])
def api_delete_position(position_id):
    """Elimina una posición"""
    try:
        portfolio = cargar_portfolio()
        
        if portfolio.eliminar_posicion(position_id):
            guardar_portfolio(portfolio)
            return jsonify({'success': True, 'message': 'Posición eliminada'})
        else:
            return jsonify({'success': False, 'error': 'Posición no encontrada'})
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/historical/<ticker>')
def api_historical(ticker):
    """Obtiene histórico de precios (Yahoo Finance o justETF como fallback)"""
    periodo = request.args.get('periodo', '6mo')
    isin = request.args.get('isin', '')  # ISIN opcional para fallback a justETF
    
    print(f"[Historical] Solicitando ticker={ticker}, isin={isin}, periodo={periodo}")
    
    historico = None
    fuente = 'yahoo'
    cambio_diario = None
    mensaje_cierre = None
    
    # Determinar si es ETF europeo
    es_etf_europeo = isin and isin[:2] in ['IE', 'LU', 'DE', 'FR', 'NL', 'GB']
    
    # Calcular cambio diario usando la lógica mejorada
    try:
        from src.scrapers import obtener_cambio_diario_con_info, obtener_cambio_diario_yahoo
        
        if es_etf_europeo and isin:
            # Para ETFs europeos, comparar JustETF y Yahoo, usar el más reciente
            info_cambio = obtener_cambio_diario_con_info(isin, ticker)
            if info_cambio:
                cambio_diario = info_cambio.get('cambio')
                mensaje_cierre = info_cambio.get('mensaje')
                fuente = info_cambio.get('fuente', '')
                if cambio_diario is not None:
                    print(f"[Historical] Cambio diario de {fuente.upper()}: {cambio_diario:.2f}% - {mensaje_cierre}")
        
        if cambio_diario is None and ticker and ticker not in ['null', 'undefined', 'none', '']:
            # Para otros activos, usar Yahoo
            cambio_diario = obtener_cambio_diario_yahoo(ticker)
            if cambio_diario is not None:
                print(f"[Historical] Cambio diario de Yahoo: {cambio_diario:.2f}%")
                # Determinar si mercado USA está cerrado
                from datetime import datetime
                ahora = datetime.now()
                es_fin_de_semana = ahora.weekday() >= 5
                hora_actual = ahora.hour
                fuera_horario = hora_actual < 15 or hora_actual >= 22  # 15:30-22:00 CET
                if es_fin_de_semana or fuera_horario:
                    mensaje_cierre = "Mercado cerrado"
    except Exception as e:
        print(f"[Historical] Error calculando cambio diario: {e}")
    
    # Para ETFs europeos: comparar Yahoo y JustETF, usar el más reciente
    if es_etf_europeo and isin:
        yahoo_historico = None
        yahoo_fecha_ultima = None
        justetf_historico = None
        justetf_fecha_ultima = None
        meses = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
        
        # Verificar si el ticker es realmente un ticker de Yahoo (no un ISIN)
        # Los ISINs tienen 12 caracteres y empiezan con 2 letras de país
        ticker_es_isin = ticker and len(ticker) == 12 and ticker[:2].isalpha() and ticker[2:].isalnum()
        ticker_valido_yahoo = ticker and ticker not in ['null', 'undefined', 'none', ''] and not ticker_es_isin
        
        # Intentar Yahoo Finance solo si hay un ticker válido (no ISIN)
        if ticker_valido_yahoo:
            print(f"[Historical] Intentando Yahoo Finance con ticker: {ticker}")
            yahoo_historico = price_fetcher.obtener_historico(ticker, periodo)
            if yahoo_historico is not None and not yahoo_historico.empty:
                yahoo_fecha_ultima = yahoo_historico.index[-1].strftime('%Y-%m-%d')
                print(f"[Historical] Yahoo Finance OK: {len(yahoo_historico)} puntos, última fecha: {yahoo_fecha_ultima}")
        
        # Intentar JustETF
        try:
            from src.scrapers import JustETFScraper
            justetf = JustETFScraper()
            
            periodo_map = {
                '1w': '1mo', '1mo': '1mo', '3mo': '3mo', 
                '6mo': '6mo', '1y': '1y', '2y': '2y',
                '5y': '5y', '10y': '5y', 'ytd': '1y', 'max': '5y'
            }
            periodo_justetf = periodo_map.get(periodo, '1y')
            
            resultado_justetf = justetf.obtener_historico(isin, periodo_justetf)
            
            if resultado_justetf and resultado_justetf.get('precios') and len(resultado_justetf['precios']) > 0:
                justetf_fecha_ultima = resultado_justetf['fechas'][-1] if resultado_justetf.get('fechas') else None
                print(f"[Historical] JustETF OK: {len(resultado_justetf['precios'])} puntos, última fecha: {justetf_fecha_ultima}")
                justetf_historico = resultado_justetf
        except Exception as e:
            print(f"[Historical] Error JustETF: {e}")
        
        # Comparar fechas y elegir el más reciente
        usar_justetf = False
        if yahoo_fecha_ultima and justetf_fecha_ultima:
            if justetf_fecha_ultima >= yahoo_fecha_ultima:
                usar_justetf = True
                print(f"[Historical] Elegido JustETF (más reciente: {justetf_fecha_ultima} vs Yahoo {yahoo_fecha_ultima})")
            else:
                print(f"[Historical] Elegido Yahoo (más reciente: {yahoo_fecha_ultima} vs JustETF {justetf_fecha_ultima})")
        elif justetf_fecha_ultima:
            usar_justetf = True
            print(f"[Historical] Solo JustETF disponible: {justetf_fecha_ultima}")
        elif yahoo_fecha_ultima:
            print(f"[Historical] Solo Yahoo disponible: {yahoo_fecha_ultima}")
        
        # Devolver datos de la fuente elegida
        if usar_justetf and justetf_historico:
            nombre_etf = justetf_historico.get('nombre', isin)
            if nombre_etf == isin:
                try:
                    info_etf = justetf.obtener_precio(isin)
                    if info_etf and info_etf.get('nombre'):
                        nombre_etf = info_etf['nombre']
                except:
                    pass
            
            # IMPORTANTE: Obtener precio ACTUAL de gettex (API quote), no del histórico (NAV)
            # El histórico (performance-chart) devuelve NAV, pero queremos precio de mercado
            precio_gettex = None
            try:
                api_url = f"https://www.justetf.com/api/etfs/{isin}/quote?locale=es&currency=EUR"
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                    'Accept': 'application/json',
                }
                response_quote = requests.get(api_url, headers=headers, timeout=10)
                
                if response_quote.status_code == 200:
                    data_quote = response_quote.json()
                    if 'latestQuote' in data_quote:
                        latest = data_quote['latestQuote']
                        if isinstance(latest, dict) and 'raw' in latest:
                            precio_gettex = float(latest['raw'])
                        elif isinstance(latest, (int, float)):
                            precio_gettex = float(latest)
                        print(f"[Historical] Precio gettex de API quote: {precio_gettex:.2f}€")
                    
                    # Obtener cambio diario directo si está disponible
                    if 'dailyChangePercent' in data_quote:
                        cambio_directo = data_quote.get('dailyChangePercent', {})
                        if isinstance(cambio_directo, dict) and 'raw' in cambio_directo:
                            cambio_diario = float(cambio_directo['raw'])
                        elif isinstance(cambio_directo, (int, float)):
                            cambio_diario = float(cambio_directo)
                        print(f"[Historical] Cambio diario de API quote: {cambio_diario:.2f}%")
            except Exception as e:
                print(f"[Historical] Error obteniendo precio gettex: {e}")
            
            # Usar precios del histórico pero reemplazar el último con precio de gettex
            precios_final = [round(p, 2) for p in justetf_historico['precios']]
            if precio_gettex:
                precios_final[-1] = round(precio_gettex, 2)
                print(f"[Historical] Reemplazando último precio NAV con gettex: {precios_final[-1]}€")
            
            # Recalcular cambio_diario si no lo obtuvimos de la API quote
            if cambio_diario is None and len(precios_final) >= 2:
                precio_hoy = precios_final[-1]
                precio_ayer = justetf_historico['precios'][-2]  # Usar NAV de ayer para comparar
                if precio_ayer > 0:
                    cambio_diario = ((precio_hoy - precio_ayer) / precio_ayer) * 100
                    print(f"[Historical] Cambio diario calculado (gettex vs NAV ayer): {cambio_diario:.2f}%")
            
            # Generar mensaje_cierre desde la fecha de JustETF
            if justetf_fecha_ultima:
                try:
                    from datetime import datetime
                    fecha_obj = datetime.strptime(justetf_fecha_ultima, '%Y-%m-%d')
                    fecha_formateada = f"{fecha_obj.day} {meses[fecha_obj.month-1]} {fecha_obj.year}"
                    ahora = datetime.now()
                    es_fin_de_semana = ahora.weekday() >= 5
                    fuera_horario = ahora.hour < 8 or ahora.hour >= 22
                    fecha_hoy = ahora.strftime('%Y-%m-%d')
                    mercado_cerrado = (justetf_fecha_ultima < fecha_hoy) or es_fin_de_semana or fuera_horario
                    if mercado_cerrado:
                        mensaje_cierre = f"Al cierre: {fecha_formateada} · Mercado cerrado"
                    else:
                        mensaje_cierre = f"Al cierre: {fecha_formateada}"
                except Exception as e:
                    print(f"[Historical] Error generando mensaje cierre: {e}")
            
            return jsonify({
                'success': True, 
                'data': {
                    'fechas': justetf_historico['fechas'],
                    'precios': precios_final,
                    'nombre': nombre_etf,
                    'cambio_diario': round(cambio_diario, 2) if cambio_diario is not None else None,
                    'mensaje_cierre': mensaje_cierre
                },
                'fuente': 'justetf'
            })
        elif yahoo_historico is not None and not yahoo_historico.empty:
            # Obtener nombre del activo
            try:
                import yfinance as yf
                stock = yf.Ticker(ticker)
                nombre = stock.info.get('longName') or stock.info.get('shortName') or ticker
            except:
                nombre = ticker
            
            # Recalcular cambio_diario desde datos de Yahoo (la fuente elegida)
            if len(yahoo_historico) >= 2:
                precio_hoy = float(yahoo_historico['Close'].iloc[-1])
                precio_ayer = float(yahoo_historico['Close'].iloc[-2])
                if precio_ayer > 0:
                    cambio_diario = ((precio_hoy - precio_ayer) / precio_ayer) * 100
                    print(f"[Historical] Cambio diario recalculado de Yahoo: {cambio_diario:.2f}%")
            
            # Generar mensaje_cierre desde la fecha de Yahoo
            if yahoo_fecha_ultima:
                try:
                    from datetime import datetime
                    fecha_obj = datetime.strptime(yahoo_fecha_ultima, '%Y-%m-%d')
                    fecha_formateada = f"{fecha_obj.day} {meses[fecha_obj.month-1]} {fecha_obj.year}"
                    ahora = datetime.now()
                    es_fin_de_semana = ahora.weekday() >= 5
                    fuera_horario = ahora.hour < 8 or ahora.hour >= 22
                    fecha_hoy = ahora.strftime('%Y-%m-%d')
                    mercado_cerrado = (yahoo_fecha_ultima < fecha_hoy) or es_fin_de_semana or fuera_horario
                    if mercado_cerrado:
                        mensaje_cierre = f"Al cierre: {fecha_formateada} · Mercado cerrado"
                    else:
                        mensaje_cierre = f"Al cierre: {fecha_formateada}"
                except Exception as e:
                    print(f"[Historical] Error generando mensaje cierre: {e}")
            
            data = {
                'fechas': [d.strftime('%Y-%m-%d') for d in yahoo_historico.index],
                'precios': [round(p, 2) for p in yahoo_historico['Close'].tolist()],
                'nombre': nombre,
                'cambio_diario': round(cambio_diario, 2) if cambio_diario is not None else None,
                'mensaje_cierre': mensaje_cierre
            }
            
            if 'Volume' in yahoo_historico.columns:
                import math
                volumen_lista = []
                for v in yahoo_historico['Volume'].tolist():
                    try:
                        if v is None or (isinstance(v, float) and math.isnan(v)):
                            volumen_lista.append(0)
                        else:
                            volumen_lista.append(int(v))
                    except:
                        volumen_lista.append(0)
                data['volumenes'] = volumen_lista
            
            print(f"[Historical] Devolviendo datos de yahoo: {len(data['precios'])} puntos, nombre: {nombre}")
            return jsonify({'success': True, 'data': data, 'fuente': 'yahoo'})
        
        # Si no hay datos de ninguna fuente
        print(f"[Historical] No hay datos de ninguna fuente para ETF europeo")
        return jsonify({'success': False, 'error': 'No hay datos históricos disponibles'})
    
    # Para activos NO europeos: usar lógica original (Yahoo primero, JustETF fallback)
    # 1. Intentar con Yahoo Finance si hay ticker válido
    if ticker and ticker not in ['null', 'undefined', 'none', '']:
        print(f"[Historical] Intentando Yahoo Finance con ticker: {ticker}")
        historico = price_fetcher.obtener_historico(ticker, periodo)
        if historico is not None and not historico.empty:
            print(f"[Historical] Yahoo Finance OK: {len(historico)} puntos")
        else:
            print(f"[Historical] Yahoo Finance sin datos")
    
    # 2. Si no hay datos de Yahoo, intentar con justETF usando el ISIN
    if (historico is None or (hasattr(historico, 'empty') and historico.empty)) and isin:
        print(f"[Historical] Intentando justETF con ISIN: {isin}")
        try:
            from src.scrapers import JustETFScraper
            justetf = JustETFScraper()
            
            # Mapear período al formato de justETF
            periodo_map = {
                '1w': '1mo',  # justETF no tiene 1 semana, usar 1 mes
                '1mo': '1mo',
                '3mo': '3mo', 
                '6mo': '6mo',
                '1y': '1y',
                '2y': '2y',
                '5y': '5y',
                '10y': '5y',  # justETF máximo 5 años, usar eso para 10y
                'ytd': '1y',
                'max': '5y'
            }
            periodo_justetf = periodo_map.get(periodo, '1y')
            
            resultado = justetf.obtener_historico(isin, periodo_justetf)
            
            if resultado and resultado.get('precios') and len(resultado['precios']) > 0:
                print(f"[Historical] justETF OK: {len(resultado['precios'])} puntos")
                # Obtener nombre del ETF
                nombre_etf = resultado.get('nombre', isin)
                if nombre_etf == isin:
                    # Intentar obtener nombre desde la info del ETF
                    try:
                        info_etf = justetf.obtener_precio(isin)
                        if info_etf and info_etf.get('nombre'):
                            nombre_etf = info_etf['nombre']
                    except:
                        pass
                
                # Convertir formato justETF a formato esperado
                return jsonify({
                    'success': True, 
                    'data': {
                        'fechas': resultado['fechas'],
                        'precios': [round(p, 2) for p in resultado['precios']],
                        'nombre': nombre_etf,
                        'cambio_diario': round(cambio_diario, 2) if cambio_diario is not None else None,
                        'mensaje_cierre': mensaje_cierre
                    },
                    'fuente': 'justetf'
                })
            else:
                print(f"[Historical] justETF sin datos")
        except Exception as e:
            print(f"[Historical] Error justETF: {e}")
    
    if historico is None or (hasattr(historico, 'empty') and historico.empty):
        print(f"[Historical] No hay datos de ninguna fuente")
        return jsonify({'success': False, 'error': 'No hay datos históricos disponibles'})
    
    # Convertir a formato para Chart.js
    data = {
        'fechas': [d.strftime('%Y-%m-%d') for d in historico.index],
        'precios': [round(p, 2) for p in historico['Close'].tolist()],
        'cambio_diario': round(cambio_diario, 2) if cambio_diario is not None else None,
        'mensaje_cierre': mensaje_cierre
    }
    
    # Añadir volumen si está disponible
    if 'Volume' in historico.columns:
        import math
        volumen_lista = []
        for v in historico['Volume'].tolist():
            try:
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    volumen_lista.append(0)
                else:
                    volumen_lista.append(int(v))
            except:
                volumen_lista.append(0)
        data['volumen'] = volumen_lista
    
    # Obtener nombre del activo
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        nombre = info.get('longName') or info.get('shortName') or ticker
        data['nombre'] = nombre
    except:
        data['nombre'] = ticker
    
    print(f"[Historical] Devolviendo datos de {fuente}: {len(data['fechas'])} puntos, nombre: {data.get('nombre', 'N/A')}")
    return jsonify({'success': True, 'data': data, 'fuente': fuente})


@app.route('/details')
def details_page():
    """Página de detalles de una posición"""
    return render_template('details.html')


@app.route('/api/stats/<ticker>')
def api_stats(ticker):
    """Obtiene estadísticas clave del activo"""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        info = stock.info
        
        # Calcular dividendo - Yahoo a veces devuelve valores incorrectos
        raw_dividend = info.get('dividendYield') or 0
        # Si viene como decimal (0.028 = 2.8%), multiplicar por 100
        # Si viene como porcentaje (2.8), no multiplicar
        # Si es mayor a 1, asumimos que ya viene en porcentaje
        if raw_dividend > 0 and raw_dividend < 1:
            dividend_yield = raw_dividend * 100
        elif raw_dividend >= 1 and raw_dividend <= 25:
            dividend_yield = raw_dividend  # Ya viene en %
        else:
            dividend_yield = None  # Valor sospechoso, ignorar
        
        return jsonify({
            'success': True,
            'data': {
                'high52w': info.get('fiftyTwoWeekHigh'),
                'low52w': info.get('fiftyTwoWeekLow'),
                'avg50d': info.get('fiftyDayAverage'),
                'avg200d': info.get('twoHundredDayAverage'),
                'volume': info.get('volume') or info.get('averageVolume'),
                'marketCap': info.get('marketCap'),
                'per': info.get('trailingPE') or info.get('forwardPE'),
                'dividendYield': dividend_yield
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/ticker/search/<isin>')
def api_search_ticker(isin):
    """Busca el ticker de Yahoo Finance para un ISIN"""
    try:
        ticker = price_fetcher.buscar_ticker_por_isin(isin)
        
        if ticker:
            return jsonify({
                'success': True,
                'data': {
                    'ticker': ticker,
                    'isin': isin
                }
            })
        else:
            return jsonify({
                'success': False,
                'error': 'No se encontró ticker para este ISIN'
            })
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================================================
# API TELEGRAM
# =============================================================================

def enviar_telegram(token: str, chat_id: str, mensaje: str) -> bool:
    """Envía un mensaje por Telegram"""
    import requests
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {
            'chat_id': chat_id,
            'text': mensaje,
            'parse_mode': 'HTML'
        }
        response = requests.post(url, data=data, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"Error enviando Telegram: {e}")
        return False


def obtener_chat_id_telegram(token: str) -> str:
    """Obtiene el chat_id del último mensaje recibido por el bot"""
    import requests
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        if data.get('ok') and data.get('result'):
            # Obtener el chat_id del último mensaje
            ultimo = data['result'][-1]
            if 'message' in ultimo:
                return str(ultimo['message']['chat']['id'])
            elif 'my_chat_member' in ultimo:
                return str(ultimo['my_chat_member']['chat']['id'])
        return None
    except Exception as e:
        print(f"Error obteniendo chat_id: {e}")
        return None


def cargar_config_telegram(user_id=None):
    """Carga la configuración de Telegram"""
    if USE_DATABASE:
        if user_id is None:
            user_id = session.get('user_id')
        
        if user_id:
            config = TelegramConfig.query.filter_by(user_id=user_id).first()
        else:
            config = TelegramConfig.query.first()
        return config
    else:
        config_file = DATA_DIR / 'telegram_config.json'
        if config_file.exists():
            try:
                with open(config_file, 'r') as f:
                    return json.load(f)
            except:
                pass
    return None


def guardar_config_telegram(token: str, chat_id: str, user_id=None):
    """Guarda la configuración de Telegram"""
    if USE_DATABASE:
        if user_id is None:
            user_id = session.get('user_id')
        
        config = TelegramConfig.query.filter_by(user_id=user_id).first() if user_id else TelegramConfig.query.first()
        if config:
            config.bot_token = token
            config.chat_id = chat_id
            config.activo = True
        else:
            config = TelegramConfig(user_id=user_id, bot_token=token, chat_id=chat_id, activo=True)
            db.session.add(config)
        db.session.commit()
    else:
        config_file = DATA_DIR / 'telegram_config.json'
        DATA_DIR.mkdir(exist_ok=True)
        with open(config_file, 'w') as f:
            json.dump({'token': token, 'chat_id': chat_id, 'activo': True}, f)


def eliminar_config_telegram(user_id=None):
    """Elimina la configuración de Telegram"""
    if USE_DATABASE:
        if user_id is None:
            user_id = session.get('user_id')
        
        if user_id:
            TelegramConfig.query.filter_by(user_id=user_id).delete()
        else:
            TelegramConfig.query.delete()
        db.session.commit()
    else:
        config_file = DATA_DIR / 'telegram_config.json'
        if config_file.exists():
            config_file.unlink()


@app.route('/api/telegram/config', methods=['GET'])
@login_required
def api_telegram_config_get():
    """Obtiene la configuración de Telegram"""
    try:
        config = cargar_config_telegram()
        
        if config:
            if USE_DATABASE:
                return jsonify({'success': True, 'data': config.to_dict()})
            else:
                return jsonify({'success': True, 'data': {
                    'configurado': True,
                    'chat_id': config.get('chat_id'),
                    'activo': config.get('activo', True),
                    'token_masked': config['token'][:8] + '...' + config['token'][-4:]
                }})
        
        return jsonify({'success': True, 'data': {'configurado': False}})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/telegram/config', methods=['POST'])
@login_required
def api_telegram_config_post():
    """Guarda la configuración de Telegram"""
    try:
        data = request.json
        token = data.get('token', '').strip()
        
        if not token:
            return jsonify({'success': False, 'error': 'Token requerido'})
        
        # Validar token intentando obtener updates
        chat_id = obtener_chat_id_telegram(token)
        
        if not chat_id:
            return jsonify({'success': False, 'error': 'Token inválido o no has enviado ningún mensaje al bot. Envía cualquier mensaje a tu bot y vuelve a intentarlo.'})
        
        # Guardar configuración
        guardar_config_telegram(token, chat_id)
        
        # Enviar mensaje de bienvenida
        mensaje = """🤖 <b>Portfolio Tracker Bot</b>

✅ ¡Configuración completada!

Recibirás notificaciones cuando:
• 📉 Un activo baje al precio objetivo
• 📈 Un activo suba al precio objetivo

<i>Configura cron-job.org para verificar alertas automáticamente.</i>"""
        
        enviar_telegram(token, chat_id, mensaje)
        
        return jsonify({'success': True, 'data': {'chat_id': chat_id}})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/telegram/config', methods=['DELETE'])
@login_required
def api_telegram_config_delete():
    """Elimina la configuración de Telegram"""
    try:
        eliminar_config_telegram()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/telegram/test', methods=['POST'])
@login_required
def api_telegram_test():
    """Envía un mensaje de prueba por Telegram"""
    try:
        config = cargar_config_telegram()
        
        if not config:
            return jsonify({'success': False, 'error': 'Telegram no configurado'})
        
        if USE_DATABASE:
            token = config.bot_token
            chat_id = config.chat_id
        else:
            token = config.get('token')
            chat_id = config.get('chat_id')
        
        mensaje = """🔔 <b>Mensaje de Prueba</b>

✅ ¡Las notificaciones funcionan correctamente!

📊 <b>Portfolio Tracker</b> te avisará cuando tus alertas se cumplan."""
        
        if enviar_telegram(token, chat_id, mensaje):
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Error enviando mensaje'})
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/cron/verificar-alertas')
def api_cron_verificar_alertas():
    """Endpoint para cron-job: verifica alertas de TODOS los usuarios y notifica por Telegram"""
    try:
        total_verificadas = 0
        total_cumplidas = 0
        usuarios_notificados = 0
        
        if USE_DATABASE:
            # Obtener todos los usuarios con Telegram configurado
            configs = TelegramConfig.query.filter_by(activo=True).all()
            
            if not configs:
                return jsonify({'success': True, 'message': 'Ningún usuario tiene Telegram configurado', 'alertas_cumplidas': 0})
            
            for config in configs:
                user_id = config.user_id
                token = config.bot_token
                chat_id = config.chat_id
                
                if not token or not chat_id:
                    continue
                
                # Cargar alertas de este usuario
                alertas = Alerta.query.filter_by(user_id=user_id, activa=True, notificada=False).all()
                
                for alerta_db in alertas:
                    total_verificadas += 1
                    
                    try:
                        # Obtener precio actual
                        ticker = alerta_db.ticker or alerta_db.isin
                        resultado = price_fetcher.obtener_precio(ticker, alerta_db.isin)
                        
                        if not resultado or not resultado.get('precio'):
                            continue
                        
                        precio_actual = float(resultado['precio'])
                        alerta_db.precio_actual = precio_actual
                        
                        # Verificar si se cumplió
                        cumplida = False
                        if alerta_db.tipo == 'baja' and precio_actual <= alerta_db.precio_objetivo:
                            cumplida = True
                        elif alerta_db.tipo == 'sube' and precio_actual >= alerta_db.precio_objetivo:
                            cumplida = True
                        
                        if cumplida:
                            alerta_db.notificada = True
                            alerta_db.disparada = True
                            alerta_db.fecha_disparo = datetime.utcnow()
                            total_cumplidas += 1
                            
                            # Enviar notificación
                            tipo_emoji = '📉' if alerta_db.tipo == 'baja' else '📈'
                            tipo_texto = 'bajado' if alerta_db.tipo == 'baja' else 'subido'
                            
                            precio_ref = alerta_db.precio_referencia or 0
                            cambio_pct = ((precio_actual - precio_ref) / precio_ref * 100) if precio_ref > 0 else 0
                            
                            mensaje = f"""🚨 <b>¡ALERTA CUMPLIDA!</b>

{tipo_emoji} <b>{alerta_db.nombre or alerta_db.isin}</b>

Ha {tipo_texto} a <b>{precio_actual:.2f}€</b>
({cambio_pct:+.2f}% desde {precio_ref:.2f}€)

🎯 Objetivo: {alerta_db.precio_objetivo:.2f}€

🛒 <i>¡Momento de actuar!</i>"""
                            
                            enviar_telegram(token, chat_id, mensaje)
                            
                    except Exception as e:
                        print(f"Error verificando alerta {alerta_db.id}: {e}")
                        continue
                
                usuarios_notificados += 1
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'usuarios_verificados': usuarios_notificados,
                'alertas_verificadas': total_verificadas,
                'alertas_cumplidas': total_cumplidas,
                'timestamp': datetime.now().isoformat()
            })
        
        else:
            # Modo local (un solo usuario)
            config = cargar_config_telegram()
            
            if not config:
                return jsonify({'success': True, 'message': 'Telegram no configurado', 'alertas_cumplidas': 0})
            
            token = config.get('token')
            chat_id = config.get('chat_id')
            activo = config.get('activo', True)
            
            if not activo:
                return jsonify({'success': True, 'message': 'Telegram desactivado', 'alertas_cumplidas': 0})
            
            alertas = cargar_alertas()
            alertas_cumplidas = []
            alertas_modificadas = False
            
            for alerta in alertas:
                if not alerta.get('activa', True) or alerta.get('notificada', False):
                    continue
                
                total_verificadas += 1
                
                try:
                    ticker = alerta.get('ticker') or alerta.get('isin')
                    resultado = price_fetcher.obtener_precio(ticker, alerta.get('isin'))
                    
                    if not resultado or not resultado.get('precio'):
                        continue
                    
                    precio_actual = float(resultado['precio'])
                    alerta['precio_actual'] = precio_actual
                    
                    cumplida = False
                    if alerta.get('tipo') == 'baja' and precio_actual <= alerta.get('precio_objetivo', 0):
                        cumplida = True
                    elif alerta.get('tipo') == 'sube' and precio_actual >= alerta.get('precio_objetivo', 0):
                        cumplida = True
                    
                    if cumplida:
                        alerta['notificada'] = True
                        alertas_modificadas = True
                        alertas_cumplidas.append(alerta)
                        total_cumplidas += 1
                        
                except Exception as e:
                    print(f"Error verificando alerta {alerta.get('id')}: {e}")
                    continue
            
            if alertas_modificadas:
                guardar_alertas(alertas)
            
            for alerta in alertas_cumplidas:
                tipo_emoji = '📉' if alerta.get('tipo') == 'baja' else '📈'
                tipo_texto = 'bajado' if alerta.get('tipo') == 'baja' else 'subido'
                
                precio_ref = alerta.get('precio_referencia', 0)
                precio_actual = alerta.get('precio_actual', 0)
                cambio_pct = ((precio_actual - precio_ref) / precio_ref * 100) if precio_ref > 0 else 0
                
                mensaje = f"""🚨 <b>¡ALERTA CUMPLIDA!</b>

{tipo_emoji} <b>{alerta.get('nombre', alerta.get('isin'))}</b>

Ha {tipo_texto} a <b>{precio_actual:.2f}€</b>
({cambio_pct:+.2f}% desde {precio_ref:.2f}€)

🎯 Objetivo: {alerta.get('precio_objetivo', 0):.2f}€

🛒 <i>¡Momento de actuar!</i>"""
                
                enviar_telegram(token, chat_id, mensaje)
            
            return jsonify({
                'success': True,
                'alertas_verificadas': total_verificadas,
                'alertas_cumplidas': total_cumplidas,
                'timestamp': datetime.now().isoformat()
            })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================================================
# ADMINISTRACIÓN DE USUARIOS
# =============================================================================

@app.route('/admin')
@admin_required
def admin_page():
    """Página de administración"""
    return render_template('admin.html')


@app.route('/api/admin/usuarios', methods=['GET'])
@admin_required
def api_admin_usuarios():
    """Lista todos los usuarios"""
    try:
        if not USE_DATABASE:
            return jsonify({'success': False, 'error': 'Requiere base de datos'})
        
        usuarios = Usuario.query.all()
        return jsonify({
            'success': True,
            'data': [u.to_dict() for u in usuarios]
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/usuarios', methods=['POST'])
@admin_required
def api_admin_crear_usuario():
    """Crea un nuevo usuario"""
    try:
        if not USE_DATABASE:
            return jsonify({'success': False, 'error': 'Requiere base de datos'})
        
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '')
        nombre = data.get('nombre', '').strip()
        is_admin = data.get('is_admin', False)
        
        if not username or not password:
            return jsonify({'success': False, 'error': 'Usuario y contraseña requeridos'})
        
        # Verificar si ya existe
        if Usuario.query.filter_by(username=username).first():
            return jsonify({'success': False, 'error': 'El usuario ya existe'})
        
        # Crear usuario
        usuario = Usuario(
            username=username,
            nombre=nombre or username,
            is_admin=is_admin,
            activo=True
        )
        usuario.set_password(password)
        db.session.add(usuario)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'data': usuario.to_dict(),
            'message': f'Usuario {username} creado correctamente'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/usuarios/<int:user_id>', methods=['PUT'])
@admin_required
def api_admin_editar_usuario(user_id):
    """Edita un usuario existente"""
    try:
        if not USE_DATABASE:
            return jsonify({'success': False, 'error': 'Requiere base de datos'})
        
        usuario = Usuario.query.get(user_id)
        if not usuario:
            return jsonify({'success': False, 'error': 'Usuario no encontrado'})
        
        data = request.json
        
        if 'nombre' in data:
            usuario.nombre = data['nombre'].strip()
        if 'activo' in data:
            usuario.activo = data['activo']
        if 'is_admin' in data:
            usuario.is_admin = data['is_admin']
        if 'password' in data and data['password']:
            usuario.set_password(data['password'])
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'data': usuario.to_dict(),
            'message': 'Usuario actualizado'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/usuarios/<int:user_id>', methods=['DELETE'])
@admin_required
def api_admin_eliminar_usuario(user_id):
    """Elimina un usuario"""
    try:
        if not USE_DATABASE:
            return jsonify({'success': False, 'error': 'Requiere base de datos'})
        
        # No permitir eliminarse a sí mismo
        if user_id == session.get('user_id'):
            return jsonify({'success': False, 'error': 'No puedes eliminarte a ti mismo'})
        
        usuario = Usuario.query.get(user_id)
        if not usuario:
            return jsonify({'success': False, 'error': 'Usuario no encontrado'})
        
        db.session.delete(usuario)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Usuario {usuario.username} eliminado'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/admin/stats')
@admin_required
def api_admin_stats():
    """Estadísticas de la plataforma"""
    try:
        if not USE_DATABASE:
            return jsonify({'success': False, 'error': 'Requiere base de datos'})
        
        total_usuarios = Usuario.query.count()
        usuarios_activos = Usuario.query.filter_by(activo=True).count()
        total_posiciones = Posicion.query.count()
        total_alertas = Alerta.query.count()
        alertas_activas = Alerta.query.filter_by(activa=True).count()
        telegram_configurados = TelegramConfig.query.filter_by(activo=True).count()
        
        return jsonify({
            'success': True,
            'data': {
                'total_usuarios': total_usuarios,
                'usuarios_activos': usuarios_activos,
                'total_posiciones': total_posiciones,
                'total_alertas': total_alertas,
                'alertas_activas': alertas_activas,
                'telegram_configurados': telegram_configurados
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='Portfolio Tracker Web App')
    parser.add_argument('--port', type=int, default=5000, help='Puerto del servidor (default: 5000)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Host del servidor (default: 0.0.0.0)')
    args = parser.parse_args()
    
    print("\n" + "="*50)
    print("📊 PORTFOLIO TRACKER - Web Dashboard")
    print("="*50)
    print(f"\n🌐 Abre tu navegador en: http://localhost:{args.port}")
    print("\n💡 Pulsa Ctrl+C para detener el servidor\n")
    
    app.run(debug=True, host=args.host, port=args.port)
