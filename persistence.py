"""
Capa de persistencia para Portfolio Tracker
Soporta archivos JSON (local) y base de datos (producción)
"""
import os
import json
from datetime import datetime

# Detectar si usamos BD o JSON
USE_DATABASE = os.environ.get('DATABASE_URL') is not None or os.environ.get('USE_DATABASE', 'false').lower() == 'true'

if USE_DATABASE:
    from database import db, Posicion, Aportacion, Alerta, Target, ActivoNuevo

# Rutas para archivos JSON (modo local)
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
PORTFOLIO_FILE = os.path.join(DATA_DIR, 'portfolio.json')
ALERTAS_FILE = os.path.join(DATA_DIR, 'alertas.json')
TARGETS_FILE = os.path.join(DATA_DIR, 'targets.json')
TARGETS_POS_FILE = os.path.join(DATA_DIR, 'targets_positions.json')
NUEVOS_FILE = os.path.join(DATA_DIR, 'nuevos_activos.json')


# ========================
# PORTFOLIO
# ========================

def cargar_portfolio_data():
    """Carga los datos del portfolio"""
    if USE_DATABASE:
        posiciones = Posicion.query.all()
        return {
            'posiciones': [p.to_dict() for p in posiciones]
        }
    else:
        if os.path.exists(PORTFOLIO_FILE):
            with open(PORTFOLIO_FILE, 'r') as f:
                return json.load(f)
        return {'posiciones': []}


def guardar_portfolio_data(data):
    """Guarda los datos del portfolio"""
    if USE_DATABASE:
        # Limpiar y re-crear
        Aportacion.query.delete()
        Posicion.query.delete()
        
        for pos_data in data.get('posiciones', []):
            pos = Posicion(
                id=pos_data.get('id', pos_data.get('isin')),
                isin=pos_data['isin'],
                ticker=pos_data.get('ticker'),
                nombre=pos_data['nombre'],
                categoria=pos_data.get('categoria')
            )
            db.session.add(pos)
            
            for ap_data in pos_data.get('aportaciones', []):
                fecha = ap_data.get('fecha')
                if isinstance(fecha, str):
                    fecha = datetime.fromisoformat(fecha).date()
                
                ap = Aportacion(
                    posicion_id=pos.id,
                    fecha=fecha or datetime.utcnow().date(),
                    cantidad=ap_data['cantidad'],
                    precio=ap_data['precio'],
                    comision=ap_data.get('comision', 0),
                    notas=ap_data.get('notas')
                )
                db.session.add(ap)
        
        db.session.commit()
    else:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(PORTFOLIO_FILE, 'w') as f:
            json.dump(data, f, indent=2, default=str)


def agregar_posicion(pos_data):
    """Agrega una nueva posición"""
    if USE_DATABASE:
        pos = Posicion(
            id=pos_data.get('id', pos_data.get('isin')),
            isin=pos_data['isin'],
            ticker=pos_data.get('ticker'),
            nombre=pos_data['nombre'],
            categoria=pos_data.get('categoria')
        )
        db.session.add(pos)
        
        for ap_data in pos_data.get('aportaciones', []):
            fecha = ap_data.get('fecha')
            if isinstance(fecha, str):
                fecha = datetime.fromisoformat(fecha).date()
            
            ap = Aportacion(
                posicion_id=pos.id,
                fecha=fecha or datetime.utcnow().date(),
                cantidad=ap_data['cantidad'],
                precio=ap_data['precio'],
                comision=ap_data.get('comision', 0)
            )
            db.session.add(ap)
        
        db.session.commit()
        return pos.to_dict()
    else:
        data = cargar_portfolio_data()
        data['posiciones'].append(pos_data)
        guardar_portfolio_data(data)
        return pos_data


def eliminar_posicion(posicion_id):
    """Elimina una posición"""
    if USE_DATABASE:
        pos = Posicion.query.get(posicion_id)
        if pos:
            db.session.delete(pos)
            db.session.commit()
            return True
        return False
    else:
        data = cargar_portfolio_data()
        data['posiciones'] = [p for p in data['posiciones'] if p.get('id') != posicion_id]
        guardar_portfolio_data(data)
        return True


def buscar_posicion(posicion_id):
    """Busca una posición por ID o ISIN"""
    if USE_DATABASE:
        pos = Posicion.query.get(posicion_id)
        if not pos:
            pos = Posicion.query.filter_by(isin=posicion_id).first()
        return pos.to_dict() if pos else None
    else:
        data = cargar_portfolio_data()
        for p in data['posiciones']:
            if p.get('id') == posicion_id or p.get('isin') == posicion_id:
                return p
        return None


def agregar_aportacion(posicion_id, ap_data):
    """Agrega una aportación a una posición"""
    if USE_DATABASE:
        pos = Posicion.query.get(posicion_id)
        if not pos:
            pos = Posicion.query.filter_by(isin=posicion_id).first()
        
        if pos:
            fecha = ap_data.get('fecha')
            if isinstance(fecha, str):
                fecha = datetime.fromisoformat(fecha).date()
            
            ap = Aportacion(
                posicion_id=pos.id,
                fecha=fecha or datetime.utcnow().date(),
                cantidad=ap_data['cantidad'],
                precio=ap_data['precio'],
                comision=ap_data.get('comision', 0)
            )
            db.session.add(ap)
            db.session.commit()
            return True
        return False
    else:
        data = cargar_portfolio_data()
        for p in data['posiciones']:
            if p.get('id') == posicion_id or p.get('isin') == posicion_id:
                if 'aportaciones' not in p:
                    p['aportaciones'] = []
                p['aportaciones'].append(ap_data)
                guardar_portfolio_data(data)
                return True
        return False


# ========================
# ALERTAS
# ========================

def cargar_alertas():
    """Carga las alertas"""
    if USE_DATABASE:
        alertas = Alerta.query.all()
        return [a.to_dict() for a in alertas]
    else:
        if os.path.exists(ALERTAS_FILE):
            with open(ALERTAS_FILE, 'r') as f:
                return json.load(f)
        return []


def guardar_alertas(alertas):
    """Guarda las alertas"""
    if USE_DATABASE:
        Alerta.query.delete()
        for al_data in alertas:
            alerta = Alerta(
                id=al_data['id'],
                isin=al_data['isin'],
                nombre=al_data.get('nombre'),
                tipo=al_data['tipo'],
                precio_objetivo=al_data['precio_objetivo'],
                precio_actual=al_data.get('precio_actual'),
                activa=al_data.get('activa', True),
                disparada=al_data.get('disparada', False)
            )
            db.session.add(alerta)
        db.session.commit()
    else:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(ALERTAS_FILE, 'w') as f:
            json.dump(alertas, f, indent=2)


def agregar_alerta(alerta_data):
    """Agrega una nueva alerta"""
    if USE_DATABASE:
        alerta = Alerta(
            id=alerta_data['id'],
            isin=alerta_data['isin'],
            nombre=alerta_data.get('nombre'),
            tipo=alerta_data['tipo'],
            precio_objetivo=alerta_data['precio_objetivo'],
            precio_actual=alerta_data.get('precio_actual'),
            activa=alerta_data.get('activa', True)
        )
        db.session.add(alerta)
        db.session.commit()
        return alerta.to_dict()
    else:
        alertas = cargar_alertas()
        alertas.append(alerta_data)
        guardar_alertas(alertas)
        return alerta_data


def eliminar_alerta(alerta_id):
    """Elimina una alerta"""
    if USE_DATABASE:
        alerta = Alerta.query.get(alerta_id)
        if alerta:
            db.session.delete(alerta)
            db.session.commit()
            return True
        return False
    else:
        alertas = cargar_alertas()
        alertas = [a for a in alertas if a.get('id') != alerta_id]
        guardar_alertas(alertas)
        return True


# ========================
# TARGETS
# ========================

def cargar_targets():
    """Carga los targets por categoría"""
    if USE_DATABASE:
        # En BD solo usamos targets por posición
        return {}
    else:
        if os.path.exists(TARGETS_FILE):
            with open(TARGETS_FILE, 'r') as f:
                return json.load(f)
        return {}


def guardar_targets(targets):
    """Guarda los targets por categoría"""
    if not USE_DATABASE:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TARGETS_FILE, 'w') as f:
            json.dump(targets, f, indent=2)


def cargar_targets_positions():
    """Carga los targets por posición (ISIN)"""
    if USE_DATABASE:
        targets = Target.query.all()
        return {t.isin: t.porcentaje for t in targets}
    else:
        if os.path.exists(TARGETS_POS_FILE):
            with open(TARGETS_POS_FILE, 'r') as f:
                return json.load(f)
        return {}


def guardar_targets_positions(targets):
    """Guarda los targets por posición"""
    if USE_DATABASE:
        Target.query.delete()
        for isin, porcentaje in targets.items():
            target = Target(isin=isin, porcentaje=porcentaje)
            db.session.add(target)
        db.session.commit()
    else:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(TARGETS_POS_FILE, 'w') as f:
            json.dump(targets, f, indent=2)


# ========================
# ACTIVOS NUEVOS
# ========================

def cargar_nuevos_activos():
    """Carga los activos nuevos planificados"""
    if USE_DATABASE:
        nuevos = ActivoNuevo.query.all()
        return {n.isin: n.to_dict() for n in nuevos}
    else:
        if os.path.exists(NUEVOS_FILE):
            with open(NUEVOS_FILE, 'r') as f:
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
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(NUEVOS_FILE, 'w') as f:
            json.dump(nuevos, f, indent=2)


# ========================
# EXPORT/IMPORT
# ========================

def exportar_todo():
    """Exporta todos los datos"""
    return {
        'posiciones': cargar_portfolio_data().get('posiciones', []),
        'alertas': cargar_alertas(),
        'targets_positions': cargar_targets_positions(),
        'targets_categorias': cargar_targets(),
        'nuevos_activos': cargar_nuevos_activos(),
        'export_date': datetime.now().isoformat(),
        'export_version': '4.0'
    }


def importar_todo(data):
    """Importa todos los datos"""
    if 'posiciones' in data:
        guardar_portfolio_data({'posiciones': data['posiciones']})
    
    if 'alertas' in data:
        guardar_alertas(data['alertas'])
    
    if 'targets_positions' in data:
        guardar_targets_positions(data['targets_positions'])
    
    if 'targets_categorias' in data and not USE_DATABASE:
        guardar_targets(data['targets_categorias'])
    
    if 'nuevos_activos' in data:
        guardar_nuevos_activos(data['nuevos_activos'])
