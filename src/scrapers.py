"""
Módulo de scraping para obtener precios de Morningstar y justETF
Usado como fallback cuando Yahoo Finance no tiene el activo
"""
import requests
from bs4 import BeautifulSoup
import re
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import time
import json

# Headers para simular navegador
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'es-ES,es;q=0.9,en;q=0.8',
    'Connection': 'keep-alive',
}

# Cache global para cambios diarios (evitar múltiples requests)
_cambio_diario_cache: Dict[str, Dict[str, Any]] = {}
_cache_timestamp: Optional[datetime] = None
_CACHE_DURATION_MINUTES = 15


def obtener_cambio_diario_yahoo(ticker_o_isin: str, es_isin: bool = False) -> Optional[float]:
    """
    Obtiene el cambio diario usando Yahoo Finance de forma robusta.
    
    Usa previousClose de fast_info que es más fiable que calcular
    desde el histórico (que falla en festivos).
    
    Args:
        ticker_o_isin: Ticker de Yahoo o ISIN
        es_isin: True si es ISIN (para intentar buscar ticker)
        
    Returns:
        float: Cambio porcentual (ej: 0.58 para +0.58%)
        None: Si no se puede obtener
    """
    global _cambio_diario_cache, _cache_timestamp
    
    cache_key = ticker_o_isin
    
    # Verificar caché
    if _cache_timestamp and (datetime.now() - _cache_timestamp).total_seconds() < _CACHE_DURATION_MINUTES * 60:
        if cache_key in _cambio_diario_cache:
            return _cambio_diario_cache[cache_key].get('cambio')
    
    try:
        import yfinance as yf
        
        stock = yf.Ticker(ticker_o_isin)
        info = stock.fast_info
        
        # Método 1: Usar previousClose y lastPrice de fast_info
        last_price = getattr(info, 'last_price', None)
        prev_close = getattr(info, 'previous_close', None)
        
        if last_price and prev_close and prev_close > 0:
            cambio = ((last_price - prev_close) / prev_close) * 100
            
            # Guardar en caché
            _cambio_diario_cache[cache_key] = {'cambio': cambio}
            _cache_timestamp = datetime.now()
            
            print(f"[Yahoo fast_info] {ticker_o_isin}: {cambio:.2f}%")
            return cambio
        
        # Método 2: Fallback al histórico pero verificando fechas
        hist = stock.history(period='5d')
        if not hist.empty and len(hist) >= 2:
            # Verificar que los dos últimos días son consecutivos (máx 4 días de diferencia por fines de semana)
            fecha_hoy = hist.index[-1]
            fecha_ayer = hist.index[-2]
            dias_diferencia = (fecha_hoy - fecha_ayer).days
            
            if dias_diferencia <= 4:  # Permitir hasta 4 días (viernes a lunes + festivo)
                precio_ayer = float(hist['Close'].iloc[-2])
                precio_hoy = float(hist['Close'].iloc[-1])
                
                if precio_ayer > 0:
                    cambio = ((precio_hoy - precio_ayer) / precio_ayer) * 100
                    
                    _cambio_diario_cache[cache_key] = {'cambio': cambio}
                    _cache_timestamp = datetime.now()
                    
                    print(f"[Yahoo history] {ticker_o_isin}: {cambio:.2f}% (gap: {dias_diferencia} días)")
                    return cambio
            else:
                print(f"[Yahoo] Gap de {dias_diferencia} días detectado para {ticker_o_isin}, datos no fiables")
                
    except Exception as e:
        print(f"[Yahoo] Error para {ticker_o_isin}: {e}")
    
    return None


def obtener_cambio_diario_justetf(isin: str) -> Optional[float]:
    """
    Obtiene el cambio diario (%) para ETFs europeos.
    
    Usa múltiples fuentes en orden de prioridad:
    1. API de JustETF
    2. Yahoo Finance con ISIN
    3. Histórico de JustETF (último recurso)
    
    Returns:
        float: Cambio porcentual (ej: 0.58 para +0.58%)
        None: Si no se puede obtener
    """
    global _cambio_diario_cache, _cache_timestamp
    
    # Verificar caché (15 minutos)
    if _cache_timestamp and (datetime.now() - _cache_timestamp).total_seconds() < _CACHE_DURATION_MINUTES * 60:
        if isin in _cambio_diario_cache:
            cached = _cambio_diario_cache[isin].get('cambio')
            if cached is not None:
                return cached
    
    # Método 1: Intentar obtener de la API de JustETF
    cambio = _obtener_cambio_api_justetf(isin)
    if cambio is not None:
        _cambio_diario_cache[isin] = {'cambio': cambio}
        _cache_timestamp = datetime.now()
        return cambio
    
    # Método 2: Intentar Yahoo Finance con el ISIN
    cambio = obtener_cambio_diario_yahoo(isin)
    if cambio is not None:
        _cambio_diario_cache[isin] = {'cambio': cambio}
        _cache_timestamp = datetime.now()
        return cambio
    
    # Método 3: Histórico de JustETF (menos preciso pero funciona)
    cambio = _obtener_cambio_historico_justetf(isin)
    if cambio is not None:
        _cambio_diario_cache[isin] = {'cambio': cambio}
        _cache_timestamp = datetime.now()
        return cambio
    
    return None


def obtener_cambio_diario_con_info(isin: str) -> Dict[str, Any]:
    """
    Obtiene el cambio diario con información adicional sobre fecha y estado del mercado.
    
    Returns:
        Dict con:
        - cambio: float o None
        - fecha_cierre: str (formato "23 dic 2025, 17:30")
        - mercado_cerrado: bool
        - mensaje: str (ej: "Al cierre: 23 dic 2025 · Mercado cerrado")
    """
    resultado = {
        'cambio': None,
        'fecha_cierre': None,
        'mercado_cerrado': True,
        'mensaje': ''
    }
    
    fecha_ultimo = None
    meses = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 
            'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
    
    try:
        # Obtener datos del histórico para tener la fecha
        url = f"https://www.justetf.com/api/etfs/{isin}/performance-chart"
        params = {
            'locale': 'es',
            'currency': 'EUR',
            'valuesType': 'MARKET_VALUE',
            'dateFrom': (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d'),
            'dateTo': datetime.now().strftime('%Y-%m-%d')
        }
        
        response = requests.get(url, params=params, headers=HEADERS, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            series = data.get('series', [])
            
            if series and len(series) >= 2:
                ultimo = series[-1]
                penultimo = series[-2]
                
                precio_hoy = ultimo.get('value', {}).get('raw', 0)
                precio_ayer = penultimo.get('value', {}).get('raw', 0)
                fecha_ultimo = ultimo.get('date', '')  # Formato: "2025-12-23"
                
                if precio_ayer > 0:
                    resultado['cambio'] = ((precio_hoy - precio_ayer) / precio_ayer) * 100
                    print(f"[Info] JustETF histórico OK para {isin}: {resultado['cambio']:.2f}%")
                    
    except Exception as e:
        print(f"[Info] Error obteniendo histórico JustETF para {isin}: {e}")
    
    # Si no obtuvimos cambio del histórico, intentar otros métodos
    if resultado['cambio'] is None:
        resultado['cambio'] = obtener_cambio_diario_justetf(isin)
        print(f"[Info] Usando fallback para {isin}: {resultado['cambio']}")
    
    # SIEMPRE generar mensaje de cierre
    ahora = datetime.now()
    es_fin_de_semana = ahora.weekday() >= 5  # Sábado=5, Domingo=6
    hora_actual = ahora.hour
    # Horario de gettex: 8:00-22:00 CET
    fuera_horario = hora_actual < 8 or hora_actual >= 22
    
    # Si tenemos fecha del histórico de JustETF, usarla
    if fecha_ultimo:
        try:
            fecha_obj = datetime.strptime(fecha_ultimo, '%Y-%m-%d')
            fecha_formateada = f"{fecha_obj.day} {meses[fecha_obj.month-1]} {fecha_obj.year}"
            resultado['fecha_cierre'] = fecha_formateada
            
            # Si la fecha del último dato es anterior a hoy, el mercado cerró
            fecha_hoy = ahora.strftime('%Y-%m-%d')
            mercado_cerrado = (fecha_ultimo < fecha_hoy) or es_fin_de_semana or fuera_horario
            resultado['mercado_cerrado'] = mercado_cerrado
            
            if mercado_cerrado:
                resultado['mensaje'] = f"Al cierre: {fecha_formateada} · Mercado cerrado"
            else:
                resultado['mensaje'] = f"Al cierre: {fecha_formateada}"
        except Exception as e:
            print(f"[Info] Error parseando fecha {fecha_ultimo}: {e}")
    
    # Si no tenemos fecha de JustETF, usar fecha de hoy o ayer según el día
    if not resultado['mensaje']:
        mercado_cerrado = es_fin_de_semana or fuera_horario
        resultado['mercado_cerrado'] = mercado_cerrado
        
        # Si es fin de semana, el último día de mercado fue el viernes
        if es_fin_de_semana:
            # Calcular el viernes anterior
            dias_desde_viernes = ahora.weekday() - 4  # viernes = 4
            if dias_desde_viernes < 0:
                dias_desde_viernes += 7
            ultimo_dia_mercado = ahora - timedelta(days=dias_desde_viernes)
            fecha_formateada = f"{ultimo_dia_mercado.day} {meses[ultimo_dia_mercado.month-1]} {ultimo_dia_mercado.year}"
            resultado['fecha_cierre'] = fecha_formateada
            resultado['mensaje'] = f"Al cierre: {fecha_formateada} · Mercado cerrado"
        elif fuera_horario:
            # Fuera de horario pero día laborable
            if hora_actual < 8:
                # Antes de apertura, mostrar cierre de ayer
                ayer = ahora - timedelta(days=1)
                fecha_formateada = f"{ayer.day} {meses[ayer.month-1]} {ayer.year}"
            else:
                # Después de cierre, mostrar cierre de hoy
                fecha_formateada = f"{ahora.day} {meses[ahora.month-1]} {ahora.year}"
            resultado['fecha_cierre'] = fecha_formateada
            resultado['mensaje'] = f"Al cierre: {fecha_formateada} · Mercado cerrado"
        else:
            # Mercado abierto
            resultado['mensaje'] = "Hoy"
    
    return resultado



def _obtener_cambio_api_justetf(isin: str) -> Optional[float]:
    """
    Intenta obtener cambio diario de JustETF.
    
    El cambio diario real es: (precio_actual - cierre_ayer) / cierre_ayer * 100
    
    JustETF muestra el precio actual de gettex comparado con el cierre del día anterior.
    """
    try:
        precio_actual = None
        cierre_ayer = None
        fecha_ultimo = None
        fecha_penultimo = None
        series = []  # Inicializar para evitar error de scope
        
        # 1. Obtener histórico para tener cierre de ayer y fechas
        try:
            url = f"https://www.justetf.com/api/etfs/{isin}/performance-chart"
            params = {
                'locale': 'es',
                'currency': 'EUR',
                'valuesType': 'MARKET_VALUE',
                'dateFrom': (datetime.now() - timedelta(days=15)).strftime('%Y-%m-%d'),
                'dateTo': datetime.now().strftime('%Y-%m-%d')
            }
            
            response = requests.get(url, params=params, headers=HEADERS, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                series = data.get('series', [])
                
                if series and len(series) >= 2:
                    ultimo = series[-1]
                    penultimo = series[-2]
                    
                    precio_ultimo_historico = ultimo.get('value', {}).get('raw', 0)
                    cierre_ayer = penultimo.get('value', {}).get('raw', 0)
                    fecha_ultimo = ultimo.get('date', '')
                    fecha_penultimo = penultimo.get('date', '')
                    
                    print(f"[JustETF API] Histórico: último={precio_ultimo_historico:.2f} ({fecha_ultimo}), penúltimo={cierre_ayer:.2f} ({fecha_penultimo})")
        except Exception as e:
            print(f"[JustETF API] Error obteniendo histórico: {e}")
        
        # 2. Intentar obtener precio actual de la API de quote
        try:
            api_url = f"https://www.justetf.com/api/etfs/{isin}/quote?locale=es&currency=EUR"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': 'application/json',
            }
            response = requests.get(api_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                if 'json' in content_type:
                    data = response.json()
                    if 'latestQuote' in data:
                        latest = data['latestQuote']
                        if isinstance(latest, dict) and 'raw' in latest:
                            precio_actual = float(latest['raw'])
                            print(f"[JustETF API] Precio actual de quote API: {precio_actual:.2f}")
                        elif isinstance(latest, (int, float)):
                            precio_actual = float(latest)
                            print(f"[JustETF API] Precio actual de quote API: {precio_actual:.2f}")
                    
                    # También verificar si hay cambio diario en la respuesta
                    if 'dailyChangePercent' in data:
                        cambio_directo = data.get('dailyChangePercent', {})
                        if isinstance(cambio_directo, dict) and 'raw' in cambio_directo:
                            cambio = float(cambio_directo['raw'])
                            print(f"[JustETF API] Cambio diario directo de API: {cambio:.2f}%")
                            return cambio
                        elif isinstance(cambio_directo, (int, float)):
                            print(f"[JustETF API] Cambio diario directo de API: {cambio_directo:.2f}%")
                            return float(cambio_directo)
        except Exception as e:
            print(f"[JustETF API] Error obteniendo quote: {e}")
        
        # 3. Calcular cambio
        # Si tenemos precio actual de la API, usarlo
        # Si no, usar el último del histórico (menos preciso)
        if precio_actual is None and series and len(series) >= 1:
            precio_actual = series[-1].get('value', {}).get('raw', 0)
            print(f"[JustETF API] Usando precio del histórico como actual: {precio_actual:.2f}")
        
        if precio_actual and cierre_ayer and cierre_ayer > 0:
            cambio = ((precio_actual - cierre_ayer) / cierre_ayer) * 100
            print(f"[JustETF API] Cambio calculado: ({precio_actual:.2f} - {cierre_ayer:.2f}) / {cierre_ayer:.2f} * 100 = {cambio:.2f}%")
            return cambio
        else:
            print(f"[JustETF API] No se pudo calcular cambio. actual={precio_actual}, ayer={cierre_ayer}")
                    
    except Exception as e:
        print(f"[JustETF API] Error general para {isin}: {e}")
    
    return None


def _obtener_cambio_historico_justetf(isin: str) -> Optional[float]:
    """Obtiene cambio diario del histórico de JustETF"""
    try:
        scraper = JustETFScraper()
        historico = scraper.obtener_historico(isin, periodo='1mo')
        
        if historico and historico.get('precios') and len(historico['precios']) >= 2:
            precios = historico['precios']
            fechas = historico.get('fechas', [])
            
            # Verificar que las fechas son consecutivas (máx 4 días)
            if len(fechas) >= 2:
                try:
                    fecha_hoy = datetime.strptime(fechas[-1], '%Y-%m-%d')
                    fecha_ayer = datetime.strptime(fechas[-2], '%Y-%m-%d')
                    dias_diferencia = (fecha_hoy - fecha_ayer).days
                    
                    if dias_diferencia > 4:
                        print(f"[JustETF histórico] Gap de {dias_diferencia} días para {isin}")
                        return None
                except:
                    pass
            
            precio_ayer = precios[-2]
            precio_hoy = precios[-1]
            
            if precio_ayer > 0:
                cambio = ((precio_hoy - precio_ayer) / precio_ayer) * 100
                print(f"[JustETF histórico] {isin}: {cambio:.2f}%")
                return cambio
                
    except Exception as e:
        print(f"[JustETF histórico] Error para {isin}: {e}")
    
    return None


def _parsear_cambio_porcentual(texto: str) -> Optional[float]:
    """
    Parsea texto de cambio porcentual.
    
    Ejemplos:
        "+0,58%" -> 0.58
        "-0,85%" -> -0.85
        "0,00%" -> 0.0
    """
    try:
        if not texto:
            return None
        
        limpio = texto.strip()
        limpio = limpio.replace('%', '').strip()
        limpio = limpio.replace('+', '')
        limpio = limpio.replace(',', '.')
        limpio = limpio.replace(' ', '')
        
        return float(limpio)
    except (ValueError, AttributeError):
        return None


def obtener_cambios_diarios_batch(isins: list) -> Dict[str, Optional[float]]:
    """
    Obtiene cambios diarios para múltiples ISINs.
    
    Args:
        isins: Lista de códigos ISIN
        
    Returns:
        Dict con {isin: cambio_porcentual}
    """
    resultados = {}
    
    for isin in isins:
        try:
            cambio = obtener_cambio_diario_justetf(isin)
            resultados[isin] = cambio
        except Exception as e:
            print(f"[Batch] Error para {isin}: {e}")
            resultados[isin] = None
    
    return resultados


class MorningstarScraper:
    """Scraper para obtener precios de Morningstar"""
    
    BASE_URL = "https://www.morningstar.es"
    SEARCH_URL = "https://www.morningstar.es/es/util/SecuritySearch.ashx"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_duration = timedelta(minutes=30)
    
    def buscar_por_isin(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Busca un fondo/ETF por ISIN en Morningstar
        
        Returns:
            Dict con: precio, moneda, nombre, fecha
            None si no encuentra
        """
        # Verificar caché
        if isin in self._cache:
            cached = self._cache[isin]
            if datetime.now() - cached['timestamp'] < self._cache_duration:
                return cached['data']
        
        try:
            # Intentar búsqueda directa en Morningstar
            search_params = {
                'q': isin,
                'limit': 5,
                'preferedList': ''
            }
            
            response = self.session.get(
                self.SEARCH_URL, 
                params=search_params,
                timeout=10
            )
            
            if response.status_code != 200:
                return None
            
            # Intentar parsear JSON
            results = None
            try:
                if response.text and response.text.strip():
                    results = response.json()
            except json.JSONDecodeError:
                # Si falla el JSON, intentar método alternativo
                return self._buscar_alternativo(isin)
            
            if not results:
                return self._buscar_alternativo(isin)
            
            # Encontrar el resultado que coincida con el ISIN
            fund_info = None
            for result in results:
                result_isin = result.get('i', '').upper()
                if result_isin == isin.upper():
                    fund_info = result
                    break
            
            if not fund_info and results:
                fund_info = results[0]
            
            if not fund_info:
                return self._buscar_alternativo(isin)
            
            # Obtener página del fondo para extraer precio
            fund_url = fund_info.get('url', '')
            
            if not fund_url:
                return None
            
            # Construir URL completa si es relativa
            if fund_url.startswith('/'):
                fund_url = self.BASE_URL + fund_url
            
            # Obtener página del fondo
            time.sleep(0.5)
            fund_response = self.session.get(fund_url, timeout=10)
            
            if fund_response.status_code != 200:
                return None
            
            soup = BeautifulSoup(fund_response.text, 'lxml')
            
            # Extraer precio
            precio = self._extraer_precio_morningstar(soup)
            
            if precio is None:
                # Devolver datos parciales si encontramos el nombre
                nombre = fund_info.get('n', isin)
                if nombre and nombre != isin:
                    data = {
                        'precio': 0.0,
                        'moneda': 'EUR',
                        'nombre': nombre,
                        'fuente': 'Morningstar (sin precio)',
                        'fecha': datetime.now().strftime('%Y-%m-%d'),
                        'tipo': fund_info.get('t', 'N/A')
                    }
                    return data
                return None
            
            data = {
                'precio': precio,
                'moneda': self._extraer_moneda_morningstar(soup),
                'nombre': fund_info.get('n', isin),
                'fuente': 'Morningstar',
                'fecha': datetime.now().strftime('%Y-%m-%d'),
                'tipo': fund_info.get('t', 'N/A')
            }
            
            # Guardar en caché
            self._cache[isin] = {
                'timestamp': datetime.now(),
                'data': data
            }
            
            return data
            
        except Exception as e:
            # No imprimir error aquí, ya lo manejamos arriba
            return None
    
    def _buscar_alternativo(self, isin: str) -> Optional[Dict[str, Any]]:
        """Método alternativo de búsqueda en Morningstar"""
        try:
            # Intentar URL directa con el ISIN
            url = f"https://www.morningstar.es/es/funds/snapshot/snapshot.aspx?id={isin}"
            response = self.session.get(url, timeout=10)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'lxml')
                
                # Buscar nombre
                nombre = isin
                h1 = soup.find('h1')
                if h1:
                    nombre = h1.get_text(strip=True)
                
                # Buscar precio
                precio = self._extraer_precio_morningstar(soup)
                
                if nombre != isin:  # Al menos encontramos algo
                    return {
                        'precio': precio or 0.0,
                        'moneda': self._extraer_moneda_morningstar(soup),
                        'nombre': nombre,
                        'fuente': 'Morningstar' if precio else 'Morningstar (sin precio)',
                        'fecha': datetime.now().strftime('%Y-%m-%d'),
                        'tipo': 'Fund'
                    }
        except Exception:
            pass
        
        return None
    
    def _extraer_precio_morningstar(self, soup: BeautifulSoup) -> Optional[float]:
        """Extrae el precio de la página de Morningstar"""
        try:
            # Intentar varios selectores que usa Morningstar
            selectores = [
                'div.last-price span.price',
                'span[data-bind*="nav"]',
                'div.price-container span.price',
                'td.line.heading span',
                '.snapshot-data-table td.line.heading',
                '.price-section .price'
            ]
            
            for selector in selectores:
                elemento = soup.select_one(selector)
                if elemento:
                    texto = elemento.get_text(strip=True)
                    precio = self._parsear_precio(texto)
                    if precio:
                        return precio
            
            # Buscar por patrón en todo el HTML
            texto_completo = soup.get_text()
            match = re.search(r'NAV[^0-9]*(\d+[,.]?\d*)', texto_completo)
            if match:
                return self._parsear_precio(match.group(1))
            
            return None
            
        except Exception:
            return None
    
    def _extraer_moneda_morningstar(self, soup: BeautifulSoup) -> str:
        """Extrae la moneda de la página"""
        try:
            # Buscar moneda en el texto
            texto = soup.get_text()
            if 'EUR' in texto:
                return 'EUR'
            elif 'USD' in texto:
                return 'USD'
            elif 'GBP' in texto:
                return 'GBP'
        except:
            pass
        return 'EUR'
    
    def _parsear_precio(self, texto: str) -> Optional[float]:
        """Convierte texto a número"""
        try:
            # Limpiar texto
            limpio = texto.strip()
            limpio = re.sub(r'[€$£\s]', '', limpio)
            
            # Manejar formato europeo (1.234,56) vs americano (1,234.56)
            if ',' in limpio and '.' in limpio:
                if limpio.rfind(',') > limpio.rfind('.'):
                    # Formato europeo: 1.234,56
                    limpio = limpio.replace('.', '').replace(',', '.')
                else:
                    # Formato americano: 1,234.56
                    limpio = limpio.replace(',', '')
            elif ',' in limpio:
                # Solo coma - puede ser decimal europeo
                limpio = limpio.replace(',', '.')
            
            return float(limpio)
        except:
            return None


class JustETFScraper:
    """Scraper para obtener precios de justETF"""
    
    BASE_URL = "https://www.justetf.com"
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_duration = timedelta(minutes=30)
    
    def buscar_por_isin(self, isin: str) -> Optional[Dict[str, Any]]:
        """
        Busca un ETF por ISIN en justETF
        
        Returns:
            Dict con: precio, moneda, nombre, fecha
            None si no encuentra
        """
        # Verificar caché
        if isin in self._cache:
            cached = self._cache[isin]
            if datetime.now() - cached['timestamp'] < self._cache_duration:
                return cached['data']
        
        try:
            # PRIMERO: Intentar obtener precio de la API (más fiable)
            precio_api = self._obtener_precio_api(isin)
            
            # SEGUNDO: Obtener nombre y otros datos del HTML
            url = f"{self.BASE_URL}/es/etf-profile.html?isin={isin}"
            response = self.session.get(url, timeout=15)
            
            nombre = isin  # Default
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'lxml')
                nombre = self._extraer_nombre(soup, isin)
            
            # Si tenemos precio de la API, usarlo
            if precio_api and precio_api > 0:
                data = {
                    'precio': precio_api,
                    'moneda': 'EUR',
                    'nombre': nombre,
                    'fuente': 'justETF',
                    'fecha': datetime.now().strftime('%Y-%m-%d'),
                    'tipo': 'ETF'
                }
                
                self._cache[isin] = {
                    'timestamp': datetime.now(),
                    'data': data
                }
                
                return data
            
            # Si no hay precio de API pero encontramos el nombre, devolver info parcial
            if nombre != isin:
                data = {
                    'precio': 0.0,
                    'moneda': 'EUR',
                    'nombre': nombre,
                    'fuente': 'justETF (sin precio)',
                    'fecha': datetime.now().strftime('%Y-%m-%d'),
                    'tipo': 'ETF',
                    'ticker_sugerido': self._extraer_ticker(soup, response.text) if response.status_code == 200 else None
                }
                return data
            
            return None
            
        except Exception as e:
            return None
            
        except Exception as e:
            print(f"[justETF] Error buscando {isin}: {e}")
            return None
    
    def _extraer_nombre(self, soup: BeautifulSoup, default: str) -> str:
        """Extrae el nombre del ETF"""
        try:
            # Buscar en h1
            h1 = soup.find('h1')
            if h1:
                texto = h1.get_text(strip=True)
                if texto and len(texto) > 5:
                    return texto
            
            # Buscar en title
            title = soup.find('title')
            if title:
                texto = title.get_text(strip=True)
                # Limpiar el título (quitar " | justETF" etc)
                if '|' in texto:
                    texto = texto.split('|')[0].strip()
                if texto and len(texto) > 5:
                    return texto
            
            # Meta og:title
            meta_title = soup.find('meta', property='og:title')
            if meta_title:
                contenido = meta_title.get('content', '')
                if contenido:
                    return contenido.split('|')[0].strip()
                    
        except Exception:
            pass
        return default
    
    def _extraer_precio_justetf(self, soup: BeautifulSoup, html_text: str) -> Optional[float]:
        """Extrae el precio de justETF"""
        try:
            # IMPORTANTE: El precio de cotización se carga con JavaScript
            # y NO está disponible en el HTML estático.
            # Lo que sí podemos extraer es información del ETF.
            
            # Evitar coger el patrimonio del fondo (ej: "12.711 m" o "12,711 m")
            # Estos vienen con "m" de millones
            
            # Buscar precio en formato "EUR XX,XX" que NO sea seguido de "m" (millones)
            # Patrón: EUR seguido de número de 2 dígitos con coma decimal
            patron_precio = r'EUR\s+(\d{1,3}[,]\d{2})(?!\s*m)'
            matches = re.findall(patron_precio, html_text)
            for match in matches:
                precio = self._parsear_precio(match)
                # ETFs europeos suelen costar entre 5€ y 500€
                if precio and 5 < precio < 500:
                    return precio
            
            # Buscar cualquier número en formato XX,XX que esté en rango de precio ETF
            patron_eu = r'\b(\d{2}[,]\d{2})\b'
            matches = re.findall(patron_eu, html_text)
            for match in matches:
                precio = self._parsear_precio(match)
                if precio and 10 < precio < 200:  # Rango típico de ETFs
                    return precio
            
            # Si no encontramos precio, devolver None
            # El precio se carga con JavaScript y no está disponible
            return None
            
        except Exception:
            return None
    
    def _extraer_ticker(self, soup: BeautifulSoup, html_text: str) -> Optional[str]:
        """Extrae el ticker del ETF para sugerirlo"""
        try:
            # Método 1: Buscar en el título de la página (suele tener el ticker)
            title = soup.find('title')
            if title:
                title_text = title.get_text()
                # Formato típico: "iShares... | QDVE | IE00B..."
                parts = title_text.split('|')
                for part in parts:
                    part = part.strip()
                    # El ticker suele ser corto (2-6 caracteres) y en mayúsculas
                    if 2 <= len(part) <= 6 and part.isupper() and part.isalnum():
                        return part
            
            # Método 2: Buscar "Ticker XXXX" o "ticker: XXXX"
            patron = r'[Tt]icker[:\s]+([A-Z0-9]{2,6})\b'
            match = re.search(patron, html_text)
            if match:
                return match.group(1)
            
            # Método 3: Buscar en la URL o meta tags
            meta_ticker = soup.find('meta', {'name': 'ticker'})
            if meta_ticker and meta_ticker.get('content'):
                return meta_ticker.get('content')
                
            # Método 4: Buscar en tablas de listados
            # justETF muestra tickers en la sección de "Listados"
            listados_section = soup.find(text=re.compile(r'Listados|Listings', re.I))
            if listados_section:
                parent = listados_section.find_parent(['div', 'section', 'table'])
                if parent:
                    # Buscar tickers comunes (terminan en .DE, .L, .PA, etc.)
                    ticker_pattern = r'\b([A-Z0-9]{2,5})\.(DE|L|PA|AS|MI|SW)\b'
                    matches = re.findall(ticker_pattern, parent.get_text())
                    if matches:
                        # Preferir .DE (Xetra) para ETFs europeos
                        for ticker, exchange in matches:
                            if exchange == 'DE':
                                return f"{ticker}.DE"
                        # Si no hay .DE, devolver el primero
                        return f"{matches[0][0]}.{matches[0][1]}"
            
        except Exception:
            pass
        return None
    
    def buscar_ticker_por_isin(self, isin: str) -> Optional[str]:
        """Busca el ticker de Yahoo Finance para un ISIN"""
        try:
            # Buscar en la página de justETF
            url = f"{self.BASE_URL}/es/etf-profile.html?isin={isin}"
            response = self.session.get(url, timeout=15)
            
            if response.status_code == 200:
                soup = BeautifulSoup(response.text, 'lxml')
                
                # Buscar en la sección de Bolsa de Valores / Stock Exchange
                # justETF lista los tickers por exchange
                text = response.text
                
                # Buscar patrón de ticker con exchange
                # Ejemplos: QDVE (Xetra), SC0J (London), etc.
                
                # Primero buscar tickers de Xetra (.DE) - más común para ETFs europeos
                xetra_pattern = r'Xetra[^<]*?([A-Z0-9]{3,5})'
                match = re.search(xetra_pattern, text, re.IGNORECASE)
                if match:
                    return f"{match.group(1)}.DE"
                
                # Buscar en tabla de listings
                # El HTML suele tener: <td>QDVE</td> cerca de <td>Xetra</td>
                tables = soup.find_all('table')
                for table in tables:
                    rows = table.find_all('tr')
                    for row in rows:
                        cells = row.find_all(['td', 'th'])
                        cell_texts = [c.get_text(strip=True) for c in cells]
                        
                        # Buscar fila con Xetra
                        for i, cell in enumerate(cell_texts):
                            if 'xetra' in cell.lower():
                                # El ticker suele estar en una celda cercana
                                for j, other_cell in enumerate(cell_texts):
                                    if j != i and 2 <= len(other_cell) <= 6 and other_cell.isupper():
                                        return f"{other_cell}.DE"
                
                # Fallback: buscar cualquier ticker válido
                ticker = self._extraer_ticker(soup, text)
                if ticker:
                    # Añadir .DE si no tiene exchange
                    if '.' not in ticker:
                        return f"{ticker}.DE"
                    return ticker
                    
        except Exception:
            pass
        
        return None
    
    def _obtener_precio_api(self, isin: str) -> Optional[float]:
        """Obtiene precio de la API interna de justETF"""
        try:
            # API interna de justETF que devuelve cotizaciones
            api_url = f"https://www.justetf.com/api/etfs/{isin}/quote?locale=es&currency=EUR"
            
            # IMPORTANTE: Usar headers específicos para JSON
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': 'application/json',  # Forzar respuesta JSON
            }
            
            response = requests.get(api_url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                # Verificar que es JSON
                content_type = response.headers.get('content-type', '')
                
                if 'json' in content_type:
                    data = response.json()
                    
                    # Formato: {"latestQuote":{"raw":36.00,"localized":"36,00"}, ...}
                    if 'latestQuote' in data:
                        latest = data['latestQuote']
                        if isinstance(latest, dict) and 'raw' in latest:
                            return float(latest['raw'])
                        elif isinstance(latest, (int, float)):
                            return float(latest)
                else:
                    # Si devuelve XML, parsearlo
                    import re
                    match = re.search(r'<latestQuote><raw>([0-9.]+)</raw>', response.text)
                    if match:
                        return float(match.group(1))
                            
        except Exception as e:
            pass
        
        return None
    
    def obtener_historico(self, isin: str, periodo: str = '1y') -> Optional[Dict[str, Any]]:
        """
        Obtiene histórico de precios de justETF
        
        Args:
            isin: ISIN del ETF
            periodo: '1mo', '3mo', '6mo', '1y', '2y', '5y', 'max'
            
        Returns:
            Dict con: fechas, precios, nombre
        """
        try:
            # Calcular fechas según período
            from datetime import datetime, timedelta
            
            hoy = datetime.now()
            periodo_dias = {
                '1mo': 30,
                '3mo': 90,
                '6mo': 180,
                '1y': 365,
                '2y': 730,
                '5y': 1825,
                '10y': 3650,
                'max': 7300  # ~20 años
            }
            dias = periodo_dias.get(periodo, 365)
            fecha_desde = hoy - timedelta(days=dias)
            
            print(f"[justETF] Intentando API para {isin}, periodo={periodo} ({dias} días)")
            
            # API de gráficos de justETF
            api_url = f"https://www.justetf.com/api/etfs/{isin}/performance-chart"
            params = {
                'locale': 'es',
                'currency': 'EUR',
                'valuesType': 'MARKET_VALUE',
                'reduceData': 'false',
                'includeDividends': 'true',
                'features': 'DIVIDENDS',
                'dateFrom': fecha_desde.strftime('%Y-%m-%d'),
                'dateTo': hoy.strftime('%Y-%m-%d')
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'es-ES,es;q=0.9',
                'Referer': f'https://www.justetf.com/es/etf-profile.html?isin={isin}',
            }
            
            response = requests.get(api_url, params=params, headers=headers, timeout=15)
            
            print(f"[justETF] API status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                
                # La estructura es: series = [{'date': '2025-06-03', 'value': {'raw': 22.26}}, ...]
                series_data = data.get('series', [])
                
                if series_data and len(series_data) > 0:
                    print(f"[justETF] Encontrados {len(series_data)} puntos de datos")
                    fechas = []
                    precios = []
                    
                    for punto in series_data:
                        if isinstance(punto, dict):
                            fecha = punto.get('date', '')
                            valor = punto.get('value', {})
                            
                            # El valor puede ser {'raw': 22.26, 'localized': '22,26'} o directamente un número
                            if isinstance(valor, dict):
                                precio = valor.get('raw', 0)
                            else:
                                precio = valor
                            
                            if fecha and precio:
                                fechas.append(fecha)
                                precios.append(round(float(precio), 2))
                    
                    if fechas and precios:
                        print(f"[justETF] API OK: {len(fechas)} puntos extraídos (precio actual: {precios[-1]}€)")
                        return {
                            'fechas': fechas,
                            'precios': precios,
                            'isin': isin,
                            'fuente': 'justETF'
                        }
                    else:
                        print(f"[justETF] No se pudieron extraer datos de la serie")
                else:
                    print(f"[justETF] Series vacía en la respuesta")
            
            return None
                            
        except Exception as e:
            print(f"[justETF] Error obteniendo histórico {isin}: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _obtener_historico_alternativo(self, isin: str, periodo: str) -> Optional[Dict[str, Any]]:
        """Intenta endpoint alternativo de justETF para histórico"""
        try:
            # Endpoint alternativo
            api_url = f"https://www.justetf.com/servlet/charting-data"
            params = {
                'isin': isin,
                'locale': 'es',
                'currency': 'EUR',
                'period': periodo,
                'type': 'MARKET_VALUE'
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
                'Accept': '*/*',
                'Referer': f'https://www.justetf.com/es/etf-profile.html?isin={isin}'
            }
            
            response = requests.get(api_url, params=params, headers=headers, timeout=15)
            
            if response.status_code == 200:
                # Intentar parsear como JSON
                try:
                    data = response.json()
                    if isinstance(data, list) and len(data) > 0:
                        fechas = []
                        precios = []
                        
                        for punto in data:
                            if isinstance(punto, dict):
                                fecha = punto.get('date', punto.get('x', ''))
                                precio = punto.get('value', punto.get('y', 0))
                            elif isinstance(punto, list) and len(punto) >= 2:
                                fecha = punto[0]
                                precio = punto[1]
                            else:
                                continue
                            
                            if fecha and precio:
                                if isinstance(fecha, (int, float)):
                                    if fecha > 10000000000:
                                        fecha = fecha / 1000
                                    fecha = datetime.fromtimestamp(fecha).strftime('%Y-%m-%d')
                                fechas.append(str(fecha)[:10])
                                precios.append(round(float(precio), 2))
                        
                        if fechas and precios:
                            return {
                                'fechas': fechas,
                                'precios': precios,
                                'isin': isin,
                                'fuente': 'justETF'
                            }
                except:
                    pass
                    
        except Exception:
            pass
        
        # Intentar scraping de la página HTML
        return self._obtener_historico_desde_html(isin, periodo)
    
    def _obtener_historico_desde_html(self, isin: str, periodo: str) -> Optional[Dict[str, Any]]:
        """Extrae datos del gráfico scrapeando la página HTML de justETF"""
        try:
            # Mapear periodo a días para generar datos sintéticos basados en el precio actual
            periodo_dias = {
                '1m': 30,
                '3m': 90,
                '6m': 180,
                '1y': 365,
                '2y': 730,
                '5y': 1825,
                '10y': 3650,
                'max': 7300
            }
            dias = periodo_dias.get(periodo, 365)
            
            # Obtener página del ETF
            url = f"https://www.justetf.com/es/etf-profile.html?isin={isin}"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'es-ES,es;q=0.9',
            }
            
            response = requests.get(url, headers=headers, timeout=15)
            
            if response.status_code != 200:
                print(f"[justETF HTML] Error status: {response.status_code}")
                return None
            
            html = response.text
            
            # Buscar datos del gráfico en el JavaScript embebido
            # justETF suele tener los datos en un objeto JavaScript
            import re
            
            # Patrón 1: Buscar chartData o similar
            patterns = [
                r'chartData\s*[=:]\s*(\[[\s\S]*?\]);',
                r'"data"\s*:\s*(\[\[[\d,.\s]+\](?:,\s*\[[\d,.\s]+\])*\])',
                r'series\s*:\s*\[\s*\{\s*data\s*:\s*(\[[\s\S]*?\])\s*\}',
                r'"series"\s*:\s*\[\s*\{\s*"data"\s*:\s*(\[[\s\S]*?\])',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, html)
                if matches:
                    for match in matches:
                        try:
                            data = json.loads(match)
                            if isinstance(data, list) and len(data) > 10:
                                fechas = []
                                precios = []
                                
                                for punto in data:
                                    if isinstance(punto, list) and len(punto) >= 2:
                                        timestamp = punto[0]
                                        precio = punto[1]
                                        
                                        if isinstance(timestamp, (int, float)) and isinstance(precio, (int, float)):
                                            if timestamp > 10000000000:
                                                timestamp = timestamp / 1000
                                            fecha = datetime.fromtimestamp(timestamp)
                                            fechas.append(fecha.strftime('%Y-%m-%d'))
                                            precios.append(round(precio, 2))
                                
                                if len(fechas) > 10:
                                    print(f"[justETF HTML] Extraídos {len(fechas)} puntos del gráfico")
                                    return {
                                        'fechas': fechas,
                                        'precios': precios,
                                        'isin': isin,
                                        'fuente': 'justETF'
                                    }
                        except:
                            continue
            
            print(f"[justETF HTML] No se encontraron datos del gráfico en el HTML")
            return None
            
        except Exception as e:
            print(f"[justETF HTML] Error: {e}")
            return None
    
    def _extraer_moneda_justetf(self, soup: BeautifulSoup) -> str:
        """Extrae la moneda"""
        try:
            texto = soup.get_text()[:3000]
            # Buscar "Fund currency" seguido de la moneda
            match = re.search(r'Fund currency[:\s]*([A-Z]{3})', texto)
            if match:
                return match.group(1)
            
            if 'EUR' in texto:
                return 'EUR'
            elif 'USD' in texto:
                return 'USD'
            elif 'GBP' in texto:
                return 'GBP'
        except:
            pass
        return 'EUR'
    
    def _parsear_precio(self, texto: str) -> Optional[float]:
        """Convierte texto a número"""
        try:
            if not texto:
                return None
            limpio = texto.strip()
            limpio = re.sub(r'[€$£\s]', '', limpio)
            limpio = re.sub(r'[A-Za-z]', '', limpio)
            
            if not limpio:
                return None
            
            if ',' in limpio and '.' in limpio:
                if limpio.rfind(',') > limpio.rfind('.'):
                    limpio = limpio.replace('.', '').replace(',', '.')
                else:
                    limpio = limpio.replace(',', '')
            elif ',' in limpio:
                limpio = limpio.replace(',', '.')
            
            valor = float(limpio)
            return valor if valor > 0 else None
        except:
            return None


# Instancias globales
morningstar_scraper = MorningstarScraper()
justetf_scraper = JustETFScraper()


def buscar_precio_alternativo(isin: str) -> Optional[Dict[str, Any]]:
    """
    Busca precio en fuentes alternativas (justETF, Morningstar)
    
    Args:
        isin: Código ISIN del activo
    
    Returns:
        Dict con datos del precio o None
    """
    # Primero intentar justETF (mejor para ETFs europeos)
    try:
        resultado = justetf_scraper.buscar_por_isin(isin)
        if resultado and (resultado.get('precio', 0) > 0 or resultado.get('nombre') != isin):
            return resultado
    except Exception:
        pass
    
    # Si falla, intentar Morningstar
    time.sleep(0.3)
    try:
        resultado = morningstar_scraper.buscar_por_isin(isin)
        if resultado and (resultado.get('precio', 0) > 0 or resultado.get('nombre') != isin):
            return resultado
    except Exception:
        pass
    
    return None
