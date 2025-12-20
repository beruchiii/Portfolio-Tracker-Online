"""
Base de datos para Portfolio Tracker
Soporta SQLite (local) y PostgreSQL (producción)
"""
import os
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

db = SQLAlchemy()

class Posicion(db.Model):
    """Modelo para posiciones de la cartera"""
    __tablename__ = 'posiciones'
    
    id = db.Column(db.String(50), primary_key=True)
    isin = db.Column(db.String(20), nullable=False, index=True)
    ticker = db.Column(db.String(20))
    nombre = db.Column(db.String(200), nullable=False)
    categoria = db.Column(db.String(100))
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    
    # Relación con aportaciones
    aportaciones = db.relationship('Aportacion', backref='posicion', lazy=True, cascade='all, delete-orphan')
    
    def to_dict(self):
        return {
            'id': self.id,
            'isin': self.isin,
            'ticker': self.ticker,
            'nombre': self.nombre,
            'categoria': self.categoria,
            'aportaciones': [a.to_dict() for a in self.aportaciones]
        }
    
    @property
    def cantidad_total(self):
        return sum(a.cantidad for a in self.aportaciones)
    
    @property
    def coste_total(self):
        return sum(a.cantidad * a.precio for a in self.aportaciones)
    
    @property
    def precio_medio(self):
        if self.cantidad_total > 0:
            return self.coste_total / self.cantidad_total
        return 0


class Aportacion(db.Model):
    """Modelo para aportaciones/compras"""
    __tablename__ = 'aportaciones'
    
    id = db.Column(db.Integer, primary_key=True)
    posicion_id = db.Column(db.String(50), db.ForeignKey('posiciones.id'), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    cantidad = db.Column(db.Float, nullable=False)
    precio = db.Column(db.Float, nullable=False)
    comision = db.Column(db.Float, default=0)
    notas = db.Column(db.Text)
    
    def to_dict(self):
        return {
            'fecha': self.fecha.isoformat() if self.fecha else None,
            'cantidad': self.cantidad,
            'precio': self.precio,
            'comision': self.comision,
            'notas': self.notas
        }


class Alerta(db.Model):
    """Modelo para alertas de precio"""
    __tablename__ = 'alertas'
    
    id = db.Column(db.String(50), primary_key=True)
    isin = db.Column(db.String(20), nullable=False, index=True)
    nombre = db.Column(db.String(200))
    tipo = db.Column(db.String(20), nullable=False)  # 'above' o 'below'
    precio_objetivo = db.Column(db.Float, nullable=False)
    precio_actual = db.Column(db.Float)
    precio_referencia = db.Column(db.Float)  # Precio cuando se creó la alerta
    objetivo_pct = db.Column(db.Float)  # Porcentaje objetivo
    ticker = db.Column(db.String(20))  # Ticker del activo
    activa = db.Column(db.Boolean, default=True)
    disparada = db.Column(db.Boolean, default=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_disparo = db.Column(db.DateTime)
    notas = db.Column(db.Text)
    
    def to_dict(self):
        return {
            'id': self.id,
            'isin': self.isin,
            'nombre': self.nombre,
            'tipo': self.tipo,
            'precio_objetivo': self.precio_objetivo,
            'precio_actual': self.precio_actual,
            'precio_referencia': self.precio_referencia,
            'objetivo_pct': self.objetivo_pct,
            'ticker': self.ticker,
            'activa': self.activa,
            'disparada': self.disparada,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_disparo': self.fecha_disparo.isoformat() if self.fecha_disparo else None,
            'notas': self.notas
        }


class Target(db.Model):
    """Modelo para objetivos de asignación por posición"""
    __tablename__ = 'targets'
    
    id = db.Column(db.Integer, primary_key=True)
    isin = db.Column(db.String(20), nullable=False, unique=True, index=True)
    porcentaje = db.Column(db.Float, nullable=False)
    
    def to_dict(self):
        return {
            'isin': self.isin,
            'porcentaje': self.porcentaje
        }


class ActivoNuevo(db.Model):
    """Modelo para activos planificados (no en cartera aún)"""
    __tablename__ = 'activos_nuevos'
    
    id = db.Column(db.Integer, primary_key=True)
    isin = db.Column(db.String(20), nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(200))
    categoria = db.Column(db.String(100))
    precio = db.Column(db.Float)
    
    def to_dict(self):
        return {
            'isin': self.isin,
            'nombre': self.nombre,
            'categoria': self.categoria,
            'precio': self.precio
        }


class Usuario(db.Model):
    """Modelo para usuarios (autenticación básica)"""
    __tablename__ = 'usuarios'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    ultimo_acceso = db.Column(db.DateTime)
    
    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)


# Funciones helper para migrar datos
def init_db(app):
    """Inicializa la base de datos"""
    db.init_app(app)
    with app.app_context():
        db.create_all()


def migrar_desde_json(app, portfolio_data, alertas_data, targets_data, nuevos_data):
    """Migra datos desde archivos JSON a la base de datos"""
    with app.app_context():
        # Migrar posiciones
        if portfolio_data and 'posiciones' in portfolio_data:
            for pos_data in portfolio_data['posiciones']:
                pos = Posicion(
                    id=pos_data.get('id', pos_data.get('isin')),
                    isin=pos_data['isin'],
                    ticker=pos_data.get('ticker'),
                    nombre=pos_data['nombre'],
                    categoria=pos_data.get('categoria')
                )
                db.session.add(pos)
                
                # Migrar aportaciones
                for ap_data in pos_data.get('aportaciones', []):
                    ap = Aportacion(
                        posicion_id=pos.id,
                        fecha=datetime.fromisoformat(ap_data['fecha']).date() if ap_data.get('fecha') else datetime.utcnow().date(),
                        cantidad=ap_data['cantidad'],
                        precio=ap_data['precio'],
                        comision=ap_data.get('comision', 0),
                        notas=ap_data.get('notas')
                    )
                    db.session.add(ap)
        
        # Migrar alertas
        if alertas_data:
            for al_data in alertas_data:
                alerta = Alerta(
                    id=al_data['id'],
                    isin=al_data['isin'],
                    nombre=al_data.get('nombre'),
                    tipo=al_data['tipo'],
                    precio_objetivo=al_data['precio_objetivo'],
                    precio_actual=al_data.get('precio_actual'),
                    activa=al_data.get('activa', True),
                    disparada=al_data.get('disparada', False),
                    notas=al_data.get('notas')
                )
                db.session.add(alerta)
        
        # Migrar targets
        if targets_data:
            for isin, porcentaje in targets_data.items():
                target = Target(isin=isin, porcentaje=porcentaje)
                db.session.add(target)
        
        # Migrar activos nuevos
        if nuevos_data:
            for isin, info in nuevos_data.items():
                nuevo = ActivoNuevo(
                    isin=isin,
                    nombre=info.get('nombre'),
                    categoria=info.get('categoria'),
                    precio=info.get('precio')
                )
                db.session.add(nuevo)
        
        db.session.commit()
        print("✅ Migración completada")
