"""
Modelos de datos para Portfolio Tracker
Versión 2.0 - Soporte para múltiples aportaciones (DCA)
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, List, Dict, Any
import json
import uuid


@dataclass
class Aportacion:
    """Representa una compra/aportación individual"""
    cantidad: float
    precio_compra: float
    fecha_compra: str
    broker: str = ""
    notas: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    
    @property
    def coste_total(self) -> float:
        return self.cantidad * self.precio_compra
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'cantidad': self.cantidad,
            'precio_compra': self.precio_compra,
            'fecha_compra': self.fecha_compra,
            'broker': self.broker,
            'notas': self.notas
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Aportacion':
        return cls(
            id=data.get('id', str(uuid.uuid4())[:8]),
            cantidad=data['cantidad'],
            precio_compra=data['precio_compra'],
            fecha_compra=data['fecha_compra'],
            broker=data.get('broker', ''),
            notas=data.get('notas', '')
        )


@dataclass
class Position:
    """
    Representa una posición en cartera (agrupada por ISIN).
    Contiene múltiples aportaciones para soporte de DCA.
    """
    isin: str
    ticker: str = ""
    nombre: str = ""
    aportaciones: List[Aportacion] = field(default_factory=list)
    moneda: str = "EUR"
    categoria: str = ""  # Tecnología, Salud, Renta Fija, etc.
    sector: str = ""     # Más específico si se quiere
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    
    @property
    def cantidad(self) -> float:
        """Cantidad total de todas las aportaciones"""
        return sum(a.cantidad for a in self.aportaciones) if self.aportaciones else 0
    
    @property
    def precio_medio(self) -> float:
        """Precio medio ponderado de compra"""
        if not self.aportaciones or self.cantidad == 0:
            return 0
        total_invertido = sum(a.coste_total for a in self.aportaciones)
        return total_invertido / self.cantidad
    
    @property
    def coste_total(self) -> float:
        """Coste total de todas las aportaciones"""
        return sum(a.coste_total for a in self.aportaciones) if self.aportaciones else 0
    
    @property
    def fecha_primera_compra(self) -> str:
        """Fecha de la primera aportación"""
        if not self.aportaciones:
            return ""
        fechas = sorted([a.fecha_compra for a in self.aportaciones])
        return fechas[0]
    
    @property
    def fecha_ultima_compra(self) -> str:
        """Fecha de la última aportación"""
        if not self.aportaciones:
            return ""
        fechas = sorted([a.fecha_compra for a in self.aportaciones])
        return fechas[-1]
    
    @property
    def num_aportaciones(self) -> int:
        """Número de aportaciones"""
        return len(self.aportaciones)
    
    # Alias para compatibilidad
    @property
    def precio_compra(self) -> float:
        return self.precio_medio
    
    @property
    def fecha_compra(self) -> str:
        return self.fecha_primera_compra
    
    @property
    def broker(self) -> str:
        if self.aportaciones:
            return self.aportaciones[-1].broker
        return ""
    
    def agregar_aportacion(self, cantidad: float, precio_compra: float, 
                           fecha_compra: str, broker: str = "", notas: str = "") -> 'Aportacion':
        """Añade una nueva aportación"""
        aportacion = Aportacion(
            cantidad=cantidad,
            precio_compra=precio_compra,
            fecha_compra=fecha_compra,
            broker=broker,
            notas=notas
        )
        self.aportaciones.append(aportacion)
        return aportacion
    
    def eliminar_aportacion(self, aportacion_id: str) -> bool:
        """Elimina una aportación por ID"""
        for i, a in enumerate(self.aportaciones):
            if a.id == aportacion_id:
                self.aportaciones.pop(i)
                return True
        return False
    
    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'isin': self.isin,
            'ticker': self.ticker,
            'nombre': self.nombre,
            'moneda': self.moneda,
            'categoria': self.categoria,
            'sector': self.sector,
            'aportaciones': [a.to_dict() for a in self.aportaciones]
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Position':
        """Carga una posición desde dict (compatible con formato antiguo)"""
        aportaciones = []
        if 'aportaciones' in data and data['aportaciones']:
            aportaciones = [Aportacion.from_dict(a) for a in data['aportaciones']]
        
        pos = cls(
            id=data.get('id', str(uuid.uuid4())[:8]),
            isin=data['isin'],
            ticker=data.get('ticker', ''),
            nombre=data.get('nombre', ''),
            moneda=data.get('moneda', 'EUR'),
            categoria=data.get('categoria', ''),
            sector=data.get('sector', ''),
            aportaciones=aportaciones
        )
        
        # Migrar formato antiguo
        if not aportaciones and data.get('cantidad', 0) > 0:
            pos.agregar_aportacion(
                cantidad=data['cantidad'],
                precio_compra=data.get('precio_compra', 0),
                fecha_compra=data.get('fecha_compra', ''),
                broker=data.get('broker', ''),
                notas=data.get('notas', '')
            )
        
        return pos


@dataclass
class PositionWithPrice:
    """Posición con precio actual calculado"""
    id: str
    isin: str
    ticker: str
    nombre: str
    cantidad: float
    precio_medio: float
    precio_actual: float
    moneda: str
    aportaciones: List[Aportacion]
    num_aportaciones: int
    fecha_primera_compra: str = ""
    fecha_ultima_compra: str = ""
    categoria: str = ""
    sector: str = ""
    
    @property
    def precio_compra(self) -> float:
        return self.precio_medio
    
    @property
    def fecha_compra(self) -> str:
        return self.fecha_primera_compra
    
    @property
    def broker(self) -> str:
        if self.aportaciones:
            return self.aportaciones[-1].broker
        return ""
    
    @property
    def coste_total(self) -> float:
        return self.cantidad * self.precio_medio
    
    @property
    def valor_actual(self) -> float:
        return self.cantidad * self.precio_actual
    
    @property
    def beneficio(self) -> float:
        return self.valor_actual - self.coste_total
    
    @property
    def rentabilidad_pct(self) -> float:
        if self.coste_total == 0:
            return 0
        return (self.beneficio / self.coste_total) * 100


@dataclass 
class Portfolio:
    """Cartera de inversión"""
    nombre: str = "Mi Cartera"
    posiciones: List[Position] = field(default_factory=list)
    fecha_creacion: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    
    def agregar_posicion(self, posicion: Position):
        """Añade una posición o fusiona si ya existe el ISIN"""
        existente = self.buscar_por_isin(posicion.isin)
        
        if existente:
            for aportacion in posicion.aportaciones:
                existente.aportaciones.append(aportacion)
            if not existente.ticker and posicion.ticker:
                existente.ticker = posicion.ticker
            if not existente.nombre and posicion.nombre:
                existente.nombre = posicion.nombre
        else:
            self.posiciones.append(posicion)
    
    def agregar_aportacion(self, isin: str, cantidad: float, precio_compra: float,
                           fecha_compra: str, broker: str = "", notas: str = "") -> bool:
        """Añade una aportación a una posición existente"""
        posicion = self.buscar_por_isin(isin)
        if posicion:
            posicion.agregar_aportacion(cantidad, precio_compra, fecha_compra, broker, notas)
            return True
        return False
    
    def buscar_por_isin(self, isin: str) -> Optional[Position]:
        for pos in self.posiciones:
            if pos.isin.upper() == isin.upper():
                return pos
        return None
    
    def obtener_posicion(self, position_id: str) -> Optional[Position]:
        for pos in self.posiciones:
            if pos.id == position_id:
                return pos
        return None
    
    def eliminar_posicion(self, posicion_id: str) -> bool:
        for i, pos in enumerate(self.posiciones):
            if pos.id == posicion_id:
                self.posiciones.pop(i)
                return True
        return False
    
    def eliminar_aportacion(self, isin: str, aportacion_id: str) -> bool:
        posicion = self.buscar_por_isin(isin)
        if posicion:
            result = posicion.eliminar_aportacion(aportacion_id)
            if result and len(posicion.aportaciones) == 0:
                self.eliminar_posicion(posicion.id)
            return result
        return False
    
    def to_dict(self) -> dict:
        return {
            'nombre': self.nombre,
            'fecha_creacion': self.fecha_creacion,
            'version': '2.0',
            'posiciones': [p.to_dict() for p in self.posiciones]
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Portfolio':
        portfolio = cls(
            nombre=data.get('nombre', 'Mi Cartera'),
            fecha_creacion=data.get('fecha_creacion', datetime.now().strftime('%Y-%m-%d'))
        )
        for pos_data in data.get('posiciones', []):
            portfolio.posiciones.append(Position.from_dict(pos_data))
        return portfolio
    
    def guardar(self, filepath: str):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
    
    @classmethod
    def cargar(cls, filepath: str) -> 'Portfolio':
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
