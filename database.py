"""
Base de datos para Portfolio Tracker - Sistema Multiusuario
Soporta SQLite (local) y PostgreSQL (producción)
"""
import os
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func

db = SQLAlchemy()


class Usuario(db.Model):
    """Modelo para usuarios"""
    __tablename__ = 'usuarios'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    nombre = db.Column(db.String(100))  # Nombre para mostrar
    is_admin = db.Column(db.Boolean, default=False)
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    ultimo_acceso = db.Column(db.DateTime)
    
    # Relaciones
    posiciones = db.relationship('Posicion', backref='usuario', lazy=True, cascade='all, delete-orphan')
    alertas = db.relationship('Alerta', backref='usuario', lazy=True, cascade='all, delete-orphan')
    targets = db.relationship('Target', backref='usuario', lazy=True, cascade='all, delete-orphan')
    activos_nuevos = db.relationship('ActivoNuevo', backref='usuario', lazy=True, cascade='all, delete-orphan')
    
    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)
    
    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'nombre': self.nombre,
            'is_admin': self.is_admin,
            'activo': self.activo,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None
        }


class Posicion(db.Model):
    """Modelo para posiciones de la cartera"""
    __tablename__ = 'posiciones'
    
    id = db.Column(db.String(50), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
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
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
    isin = db.Column(db.String(20), nullable=False, index=True)
    nombre = db.Column(db.String(200))
    tipo = db.Column(db.String(20), nullable=False)  # 'baja' o 'sube'
    precio_objetivo = db.Column(db.Float, nullable=False)
    precio_actual = db.Column(db.Float)
    precio_referencia = db.Column(db.Float)
    objetivo_pct = db.Column(db.Float)
    ticker = db.Column(db.String(20))
    activa = db.Column(db.Boolean, default=True)
    disparada = db.Column(db.Boolean, default=False)
    notificada = db.Column(db.Boolean, default=False)
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
            'notificada': self.notificada,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_disparo': self.fecha_disparo.isoformat() if self.fecha_disparo else None,
            'notas': self.notas
        }


class Target(db.Model):
    """Modelo para objetivos de asignación por posición"""
    __tablename__ = 'targets'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
    isin = db.Column(db.String(20), nullable=False, index=True)
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
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, index=True)
    isin = db.Column(db.String(20), nullable=False, index=True)
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


class TelegramConfig(db.Model):
    """Modelo para configuración de Telegram por usuario"""
    __tablename__ = 'telegram_config'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True, unique=True, index=True)
    bot_token = db.Column(db.String(100), nullable=False)
    chat_id = db.Column(db.String(50))
    activo = db.Column(db.Boolean, default=True)
    fecha_configuracion = db.Column(db.DateTime, default=datetime.utcnow)
    ultima_notificacion = db.Column(db.DateTime)
    
    def to_dict(self):
        return {
            'configurado': True,
            'chat_id': self.chat_id,
            'activo': self.activo,
            'token_masked': self.bot_token[:8] + '...' + self.bot_token[-4:] if self.bot_token else None
        }


# Funciones helper
def init_db(app):
    """Inicializa la base de datos"""
    db.init_app(app)
    with app.app_context():
        db.create_all()


def crear_usuario_admin(app, username, password, nombre=None):
    """Crea el usuario administrador inicial"""
    with app.app_context():
        # Verificar si ya existe
        admin = Usuario.query.filter_by(username=username).first()
        if not admin:
            admin = Usuario(
                username=username,
                nombre=nombre or 'Administrador',
                is_admin=True,
                activo=True
            )
            admin.set_password(password)
            db.session.add(admin)
            db.session.commit()
            print(f"✅ Usuario admin '{username}' creado")
            return admin
        else:
            print(f"ℹ️ Usuario admin '{username}' ya existe")
            return admin


def migrar_datos_a_usuario(user_id):
    """Migra datos sin user_id al usuario especificado"""
    # Migrar posiciones
    Posicion.query.filter_by(user_id=None).update({'user_id': user_id})
    # Migrar alertas
    Alerta.query.filter_by(user_id=None).update({'user_id': user_id})
    # Migrar targets
    Target.query.filter_by(user_id=None).update({'user_id': user_id})
    # Migrar activos nuevos
    ActivoNuevo.query.filter_by(user_id=None).update({'user_id': user_id})
    # Migrar telegram config
    TelegramConfig.query.filter_by(user_id=None).update({'user_id': user_id})
    
    db.session.commit()
    print(f"✅ Datos migrados al usuario {user_id}")
