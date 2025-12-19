"""
Módulo de informes y cálculos de cartera
"""
from typing import List, Dict, Any, Optional
from datetime import datetime
from .models import Portfolio, Position, PositionWithPrice
from .price_fetcher import price_fetcher


class PortfolioAnalyzer:
    """Analiza y genera informes de la cartera"""
    
    def __init__(self, portfolio: Portfolio):
        self.portfolio = portfolio
        self._posiciones_con_precio: List[PositionWithPrice] = []
    
    def actualizar_precios(self) -> List[PositionWithPrice]:
        """Actualiza los precios de todas las posiciones"""
        self._posiciones_con_precio = []
        
        for pos in self.portfolio.posiciones:
            # Pasar tanto ticker como ISIN para el sistema de fallback
            precio_data = price_fetcher.obtener_precio(pos.ticker, pos.isin)
            
            # Si no hay precio o es 0, intentar con justETF directamente por ISIN
            if (not precio_data or precio_data.get('precio', 0) == 0) and pos.isin:
                precio_data_alt = price_fetcher.obtener_precio_por_isin(pos.isin)
                if precio_data_alt and precio_data_alt.get('precio', 0) > 0:
                    precio_data = precio_data_alt
            
            pos_with_price = PositionWithPrice(
                id=pos.id,
                isin=pos.isin,
                ticker=pos.ticker,
                nombre=pos.nombre,
                cantidad=pos.cantidad,
                precio_medio=pos.precio_medio,
                precio_actual=precio_data['precio'] if precio_data and precio_data.get('precio', 0) > 0 else 0.0,
                moneda=precio_data['moneda'] if precio_data else 'EUR',
                aportaciones=pos.aportaciones,
                num_aportaciones=pos.num_aportaciones,
                fecha_primera_compra=pos.fecha_primera_compra,
                fecha_ultima_compra=pos.fecha_ultima_compra,
                categoria=pos.categoria,
                sector=pos.sector
            )
            
            # Actualizar nombre si lo obtuvimos de la fuente
            if precio_data and precio_data.get('nombre') and pos.nombre == pos.isin:
                pos_with_price.nombre = precio_data['nombre']
            
            self._posiciones_con_precio.append(pos_with_price)
        
        return self._posiciones_con_precio
    
    @property
    def posiciones_con_precio(self) -> List[PositionWithPrice]:
        """Devuelve las posiciones con precios actualizados"""
        if not self._posiciones_con_precio:
            self.actualizar_precios()
        return self._posiciones_con_precio
    
    def resumen_cartera(self) -> Dict[str, Any]:
        """Genera un resumen general de la cartera"""
        posiciones = self.posiciones_con_precio
        
        if not posiciones:
            return {
                'total_invertido': 0,
                'valor_actual': 0,
                'beneficio_total': 0,
                'rentabilidad_pct': 0,
                'num_posiciones': 0,
                'posiciones_ganadoras': 0,
                'posiciones_perdedoras': 0
            }
        
        total_invertido = sum(p.coste_total for p in posiciones)
        valor_actual = sum(p.valor_actual for p in posiciones)
        beneficio_total = valor_actual - total_invertido
        rentabilidad_pct = (beneficio_total / total_invertido * 100) if total_invertido > 0 else 0
        
        ganadoras = sum(1 for p in posiciones if p.beneficio > 0)
        perdedoras = sum(1 for p in posiciones if p.beneficio < 0)
        
        return {
            'total_invertido': total_invertido,
            'valor_actual': valor_actual,
            'beneficio_total': beneficio_total,
            'rentabilidad_pct': rentabilidad_pct,
            'num_posiciones': len(posiciones),
            'posiciones_ganadoras': ganadoras,
            'posiciones_perdedoras': perdedoras
        }
    
    def mejor_posicion(self) -> Optional[PositionWithPrice]:
        """Devuelve la posición con mayor rentabilidad porcentual"""
        posiciones = self.posiciones_con_precio
        if not posiciones:
            return None
        return max(posiciones, key=lambda p: p.rentabilidad_pct)
    
    def peor_posicion(self) -> Optional[PositionWithPrice]:
        """Devuelve la posición con menor rentabilidad porcentual"""
        posiciones = self.posiciones_con_precio
        if not posiciones:
            return None
        return min(posiciones, key=lambda p: p.rentabilidad_pct)
    
    def distribucion_por_broker(self) -> Dict[str, float]:
        """Calcula la distribución del valor por broker"""
        posiciones = self.posiciones_con_precio
        distribucion = {}
        
        for pos in posiciones:
            broker = pos.broker or "Sin especificar"
            distribucion[broker] = distribucion.get(broker, 0) + pos.valor_actual
        
        return distribucion
    
    def top_posiciones(self, n: int = 5, por: str = 'valor') -> List[PositionWithPrice]:
        """
        Devuelve las top N posiciones.
        
        Args:
            n: Número de posiciones
            por: 'valor' (valor actual), 'rentabilidad' (%), 'beneficio' (€)
        """
        posiciones = self.posiciones_con_precio
        
        if por == 'valor':
            key = lambda p: p.valor_actual
        elif por == 'rentabilidad':
            key = lambda p: p.rentabilidad_pct
        else:  # beneficio
            key = lambda p: p.beneficio
        
        return sorted(posiciones, key=key, reverse=True)[:n]
    
    def calcular_peso_posiciones(self) -> List[Dict[str, Any]]:
        """Calcula el peso de cada posición en la cartera"""
        posiciones = self.posiciones_con_precio
        valor_total = sum(p.valor_actual for p in posiciones)
        
        if valor_total == 0:
            return []
        
        resultado = []
        for pos in posiciones:
            peso = (pos.valor_actual / valor_total) * 100
            resultado.append({
                'posicion': pos,
                'peso_pct': peso
            })
        
        return sorted(resultado, key=lambda x: x['peso_pct'], reverse=True)
    
    def historico_precios(self, ticker: str, periodo: str = "1y") -> Optional[Any]:
        """Obtiene el histórico de precios de una posición"""
        return price_fetcher.obtener_historico(ticker, periodo)
