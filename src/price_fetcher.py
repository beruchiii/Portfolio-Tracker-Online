"""
Módulo para obtener cotizaciones de múltiples fuentes
Sistema de fallback: Yahoo Finance → Morningstar → justETF
"""
import yfinance as yf
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from rich.console import Console

console = Console()


class PriceFetcher:
    """Obtiene precios de activos desde múltiples fuentes"""
    
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_duration = timedelta(minutes=15)
        self._scrapers_disponibles = False
        self._inicializar_scrapers()
    
    def _inicializar_scrapers(self):
        """Inicializa los scrapers de forma lazy"""
        try:
            from .scrapers import morningstar_scraper, justetf_scraper, buscar_precio_alternativo
            self._morningstar = morningstar_scraper
            self._justetf = justetf_scraper
            self._buscar_alternativo = buscar_precio_alternativo
            self._scrapers_disponibles = True
        except ImportError:
            # Silencioso - no mostrar error
            self._scrapers_disponibles = False
    
    def obtener_precio(self, ticker: str, isin: str = None) -> Optional[Dict[str, Any]]:
        """
        Obtiene el precio actual de un activo.
        
        Sistema de fallback:
        1. Yahoo Finance (por ticker)
        2. Morningstar (por ISIN)
        3. justETF (por ISIN)
        
        Args:
            ticker: Símbolo del activo en Yahoo Finance
            isin: Código ISIN (opcional, para búsqueda alternativa)
        
        Returns:
            Dict con: precio, moneda, nombre, fuente, etc.
            None si no encuentra en ninguna fuente
        """
        cache_key = f"{ticker}_{isin}" if isin else ticker
        
        # Verificar caché
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if datetime.now() - cached['timestamp'] < self._cache_duration:
                return cached['data']
        
        # 1️⃣ Intentar Yahoo Finance primero (si hay ticker válido)
        resultado = None
        if ticker and ticker.strip():
            resultado = self._obtener_yahoo(ticker)
        
        # 2️⃣ Si falla y tenemos ISIN, intentar fuentes alternativas
        if resultado is None and isin and self._scrapers_disponibles:
            resultado = self._buscar_alternativo(isin)
        
        # Guardar en caché si encontramos algo
        if resultado:
            self._cache[cache_key] = {
                'timestamp': datetime.now(),
                'data': resultado
            }
        
        return resultado
    
    def _obtener_yahoo(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Obtiene precio de Yahoo Finance"""
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            
            # Intentar obtener el precio de diferentes campos
            precio = (
                info.get('regularMarketPrice') or 
                info.get('currentPrice') or
                info.get('navPrice') or  # Para fondos
                info.get('previousClose')
            )
            
            if precio is None:
                # Intentar con historial
                hist = stock.history(period="1d")
                if not hist.empty:
                    precio = hist['Close'].iloc[-1]
            
            if precio is None:
                return None
            
            return {
                'precio': float(precio),
                'moneda': info.get('currency', 'EUR'),
                'nombre': info.get('shortName') or info.get('longName', ticker),
                'cambio_dia': info.get('regularMarketChange', 0),
                'cambio_dia_pct': info.get('regularMarketChangePercent', 0),
                'mercado': info.get('exchange', 'N/A'),
                'tipo': info.get('quoteType', 'N/A'),
                'fuente': 'Yahoo Finance'
            }
            
        except Exception as e:
            # No mostrar error aquí, lo manejamos en el nivel superior
            return None
    
    def obtener_precio_por_isin(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Obtiene precio directamente por ISIN (sin ticker de Yahoo).
        Útil para fondos que no están en Yahoo Finance.
        
        Args:
            isin: Código ISIN del activo
        
        Returns:
            Dict con datos del precio o None
        """
        # Verificar caché
        cache_key = f"isin_{isin}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if datetime.now() - cached['timestamp'] < self._cache_duration:
                return cached['data']
        
        if not self._scrapers_disponibles:
            console.print("[yellow]⚠ Scrapers no disponibles para búsqueda por ISIN[/yellow]")
            return None
        
        resultado = self._buscar_alternativo(isin)
        
        if resultado:
            self._cache[cache_key] = {
                'timestamp': datetime.now(),
                'data': resultado
            }
        
        return resultado
    
    def obtener_precios_batch(self, activos: list) -> Dict[str, Optional[Dict[str, Any]]]:
        """
        Obtiene precios de múltiples activos.
        
        Args:
            activos: Lista de dicts con 'ticker' e 'isin'
        
        Returns:
            Dict con ticker como clave y datos de precio como valor
        """
        resultados = {}
        for activo in activos:
            ticker = activo.get('ticker', '')
            isin = activo.get('isin', '')
            key = ticker or isin
            resultados[key] = self.obtener_precio(ticker, isin)
        return resultados
    
    def buscar_ticker(self, query: str) -> list:
        """
        Busca tickers que coincidan con una consulta.
        """
        try:
            ticker = yf.Ticker(query)
            info = ticker.info
            
            if info and info.get('regularMarketPrice'):
                return [{
                    'ticker': query,
                    'nombre': info.get('shortName', query),
                    'tipo': info.get('quoteType', 'N/A'),
                    'mercado': info.get('exchange', 'N/A')
                }]
        except:
            pass
        
        return []
    
    def obtener_historico(self, ticker: str, periodo: str = "1y") -> Optional[Any]:
        """
        Obtiene datos históricos de un ticker.
        
        Args:
            ticker: Símbolo del activo
            periodo: '1d', '5d', '1mo', '3mo', '6mo', '1y', '2y', '5y', '10y', 'ytd', 'max'
        
        Returns:
            DataFrame con histórico o None
        """
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period=periodo)
            return hist if not hist.empty else None
        except Exception as e:
            console.print(f"[red]Error obteniendo histórico de {ticker}: {e}[/red]")
            return None
    
    def validar_ticker(self, ticker: str) -> bool:
        """Verifica si un ticker es válido en Yahoo Finance"""
        try:
            stock = yf.Ticker(ticker)
            info = stock.info
            return bool(
                info.get('regularMarketPrice') or 
                info.get('currentPrice') or
                info.get('previousClose')
            )
        except:
            return False
    
    def validar_isin(self, isin: str) -> bool:
        """
        Verifica si un ISIN es válido buscando en fuentes alternativas.
        """
        if not self._scrapers_disponibles:
            return False
        
        resultado = self._buscar_alternativo(isin)
        return resultado is not None
    
    def buscar_ticker_por_isin(self, isin: str) -> Optional[str]:
        """
        Busca el ticker de Yahoo Finance para un ISIN.
        Útil para poder mostrar gráficos históricos.
        
        Args:
            isin: Código ISIN del activo
        
        Returns:
            Ticker de Yahoo Finance o None
        """
        if not self._scrapers_disponibles:
            return None
        
        # Método 1: Buscar en justETF
        ticker = self._justetf.buscar_ticker_por_isin(isin)
        if ticker:
            # Verificar que el ticker funciona en Yahoo Finance
            if self.validar_ticker(ticker):
                return ticker
            
            # Si no funciona, probar sin el exchange
            base_ticker = ticker.split('.')[0] if '.' in ticker else ticker
            
            # Probar con diferentes exchanges
            exchanges = ['.DE', '.L', '.PA', '.AS', '.MI', '.SW', '.F', '']
            for ex in exchanges:
                test_ticker = f"{base_ticker}{ex}" if ex else base_ticker
                if self.validar_ticker(test_ticker):
                    return test_ticker
        
        # Método 2: Buscar directamente en Yahoo Finance con el ISIN
        try:
            stock = yf.Ticker(isin)
            info = stock.info
            if info.get('symbol') and info.get('symbol') != isin:
                return info.get('symbol')
        except:
            pass
        
        return None
    
    def limpiar_cache(self):
        """Limpia la caché de precios"""
        self._cache.clear()
        console.print("[green]✓ Caché limpiada[/green]")


# Instancia global
price_fetcher = PriceFetcher()
