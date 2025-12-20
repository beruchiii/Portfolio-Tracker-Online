"""
Configuración para Portfolio Tracker
"""
import os

class Config:
    """Configuración base"""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    
    # Base de datos
    # En producción usar PostgreSQL, en desarrollo SQLite
    DATABASE_URL = os.environ.get('DATABASE_URL', '')
    
    if DATABASE_URL:
        # Render usa postgres://, SQLAlchemy necesita postgresql://
        if DATABASE_URL.startswith('postgres://'):
            DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        SQLALCHEMY_DATABASE_URI = DATABASE_URL
    else:
        # SQLite local para desarrollo
        SQLALCHEMY_DATABASE_URI = 'sqlite:///portfolio.db'
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Autenticación
    REQUIRE_AUTH = os.environ.get('REQUIRE_AUTH', 'false').lower() == 'true'
    ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
    ADMIN_PASS = os.environ.get('ADMIN_PASS', 'portfolio2024')
    
    # Cache
    CACHE_TIMEOUT = int(os.environ.get('CACHE_TIMEOUT', 300))  # 5 minutos


class DevelopmentConfig(Config):
    """Configuración de desarrollo"""
    DEBUG = True
    REQUIRE_AUTH = False


class ProductionConfig(Config):
    """Configuración de producción"""
    DEBUG = False
    REQUIRE_AUTH = True


def get_config():
    """Obtiene la configuración según el entorno"""
    env = os.environ.get('FLASK_ENV', 'development')
    if env == 'production':
        return ProductionConfig()
    return DevelopmentConfig()
