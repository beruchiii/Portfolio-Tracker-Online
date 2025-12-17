#!/usr/bin/env python3
"""
Portfolio Tracker - Interfaz de línea de comandos visual
"""
import os
import sys
from pathlib import Path
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich import box
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Prompt, Confirm
from InquirerPy import inquirer
from InquirerPy.separator import Separator

# Añadir el directorio src al path
sys.path.insert(0, str(Path(__file__).parent))

from src.models import Portfolio, Position
from src.reports import PortfolioAnalyzer
from src.price_fetcher import price_fetcher

# Configuración
console = Console()
DATA_DIR = Path(__file__).parent / "data"
PORTFOLIO_FILE = DATA_DIR / "portfolio.json"


def clear_screen():
    """Limpia la pantalla"""
    os.system('cls' if os.name == 'nt' else 'clear')


def mostrar_cabecera():
    """Muestra la cabecera de la aplicación"""
    console.print()
    console.print(Panel.fit(
        "[bold blue]📊 PORTFOLIO TRACKER[/bold blue]\n"
        "[dim]Seguimiento de tu cartera de inversiones[/dim]",
        border_style="blue",
        padding=(1, 4)
    ))
    console.print()


def cargar_portfolio() -> Portfolio:
    """Carga el portfolio desde el archivo"""
    DATA_DIR.mkdir(exist_ok=True)
    if PORTFOLIO_FILE.exists():
        return Portfolio.cargar(str(PORTFOLIO_FILE))
    return Portfolio()


def guardar_portfolio(portfolio: Portfolio):
    """Guarda el portfolio en el archivo"""
    DATA_DIR.mkdir(exist_ok=True)
    portfolio.guardar(str(PORTFOLIO_FILE))


def mostrar_resumen(portfolio: Portfolio):
    """Muestra el resumen de la cartera"""
    clear_screen()
    mostrar_cabecera()
    
    if not portfolio.posiciones:
        console.print(Panel(
            "[yellow]📭 No tienes posiciones en tu cartera[/yellow]\n\n"
            "Añade tu primera posición desde el menú principal.",
            title="Cartera vacía",
            border_style="yellow"
        ))
        console.print()
        inquirer.select(
            message="",
            choices=["← Volver al menú"]
        ).execute()
        return
    
    console.print("[bold]🔄 Actualizando cotizaciones...[/bold]")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task("Obteniendo precios...", total=None)
        analyzer = PortfolioAnalyzer(portfolio)
        posiciones = analyzer.actualizar_precios()
        progress.update(task, completed=True)
    
    resumen = analyzer.resumen_cartera()
    
    # Panel de resumen general
    color_beneficio = "green" if resumen['beneficio_total'] >= 0 else "red"
    signo = "+" if resumen['beneficio_total'] >= 0 else ""
    
    resumen_text = (
        f"[bold]💰 Valor Total:[/bold] {resumen['valor_actual']:,.2f} €\n"
        f"[bold]💵 Total Invertido:[/bold] {resumen['total_invertido']:,.2f} €\n"
        f"[bold]📈 Beneficio:[/bold] [{color_beneficio}]{signo}{resumen['beneficio_total']:,.2f} € "
        f"({signo}{resumen['rentabilidad_pct']:.2f}%)[/{color_beneficio}]\n\n"
        f"[dim]📊 Posiciones: {resumen['num_posiciones']} | "
        f"✅ Ganadoras: {resumen['posiciones_ganadoras']} | "
        f"❌ Perdedoras: {resumen['posiciones_perdedoras']}[/dim]"
    )
    
    console.print(Panel(
        resumen_text,
        title="[bold]📋 Resumen de Cartera[/bold]",
        border_style="blue",
        padding=(1, 2)
    ))
    console.print()
    
    # Tabla de posiciones
    table = Table(
        title="📌 Posiciones",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan"
    )
    
    table.add_column("Nombre", style="white", no_wrap=True, max_width=25)
    table.add_column("Ticker", style="dim")
    table.add_column("Cantidad", justify="right")
    table.add_column("P.Compra", justify="right")
    table.add_column("P.Actual", justify="right")
    table.add_column("Valor", justify="right", style="bold")
    table.add_column("Benef.", justify="right")
    table.add_column("Rent.%", justify="right")
    
    for pos in posiciones:
        color = "green" if pos.beneficio >= 0 else "red"
        signo = "+" if pos.beneficio >= 0 else ""
        
        table.add_row(
            pos.nombre[:25],
            pos.ticker,
            f"{pos.cantidad:,.2f}",
            f"{pos.precio_compra:,.2f} €",
            f"{pos.precio_actual:,.2f} €",
            f"{pos.valor_actual:,.2f} €",
            f"[{color}]{signo}{pos.beneficio:,.2f} €[/{color}]",
            f"[{color}]{signo}{pos.rentabilidad_pct:.2f}%[/{color}]"
        )
    
    console.print(table)
    console.print()
    
    inquirer.select(
        message="",
        choices=["← Volver al menú"]
    ).execute()


def agregar_posicion(portfolio: Portfolio):
    """Añade una nueva posición a la cartera"""
    
    while True:  # Bucle para permitir reintentar si el activo no es correcto
        clear_screen()
        mostrar_cabecera()
        
        console.print(Panel(
            "[bold]➕ Nueva Posición[/bold]\n\n"
            "[dim]Introduce los datos de la posición que quieres añadir.[/dim]",
            border_style="green"
        ))
        console.print()
        
        # ISIN
        isin = inquirer.text(
            message="ISIN del activo (o 'salir' para cancelar):",
            validate=lambda x: len(x) >= 4,
            invalid_message="El ISIN debe tener al menos 4 caracteres"
        ).execute()
        
        if not isin or isin.lower() == 'salir':
            return
        
        isin = isin.upper().strip()
        
        # Ticker (ahora opcional)
        console.print("\n[dim]💡 Tip: Busca el ticker en Yahoo Finance (ej: AAPL, MSFT, QDVE.DE)[/dim]")
        console.print("[dim]   Déjalo vacío si el fondo no está en Yahoo Finance[/dim]")
        ticker = inquirer.text(
            message="Ticker de Yahoo Finance (opcional):",
            default=""
        ).execute()
        
        ticker = ticker.upper().strip() if ticker else ""
        
        # Buscar información del activo
        precio_data = None
        
        console.print("\n[bold]🔍 Buscando información del activo...[/bold]")
        
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task("Buscando...", total=None)
            
            if ticker:
                precio_data = price_fetcher.obtener_precio(ticker, isin)
            else:
                precio_data = price_fetcher.obtener_precio_por_isin(isin)
            
            progress.update(task, completed=True)
        
        console.print()
        
        # Mostrar resultado de la búsqueda
        if precio_data:
            # Verificar si tiene precio real o solo info básica
            tiene_precio = precio_data.get('precio', 0) > 0
            ticker_sugerido = precio_data.get('ticker_sugerido', '')
            
            if tiene_precio:
                console.print(Panel(
                    f"[bold green]✓ Activo encontrado[/bold green]\n\n"
                    f"[bold]Nombre:[/bold] {precio_data['nombre']}\n"
                    f"[bold]Precio actual:[/bold] {precio_data['precio']:.2f} {precio_data['moneda']}\n"
                    f"[bold]Fuente:[/bold] {precio_data.get('fuente', 'N/A')}\n"
                    f"[bold]Tipo:[/bold] {precio_data.get('tipo', 'N/A')}",
                    title=f"📊 {isin}",
                    border_style="green"
                ))
            else:
                # Info encontrada pero sin precio
                nota_ticker = ""
                if ticker_sugerido:
                    nota_ticker = f"\n\n[bold cyan]💡 Sugerencia:[/bold cyan] Vuelve a intentar usando el ticker [bold]{ticker_sugerido}.DE[/bold] para obtener precio de Yahoo Finance"
                
                console.print(Panel(
                    f"[bold yellow]⚠ Activo identificado (sin precio en tiempo real)[/bold yellow]\n\n"
                    f"[bold]Nombre:[/bold] {precio_data['nombre']}\n"
                    f"[bold]Fuente:[/bold] {precio_data.get('fuente', 'N/A')}\n"
                    f"[bold]Tipo:[/bold] {precio_data.get('tipo', 'N/A')}"
                    f"{nota_ticker}",
                    title=f"📊 {isin}",
                    border_style="yellow"
                ))
        else:
            console.print(Panel(
                f"[bold yellow]⚠ No se encontró información automática[/bold yellow]\n\n"
                f"[dim]ISIN: {isin}[/dim]\n"
                f"[dim]Ticker: {ticker or '(vacío)'}[/dim]\n\n"
                "[dim]Puedes continuar introduciendo los datos manualmente.[/dim]",
                title="🔍 Búsqueda",
                border_style="yellow"
            ))
        
        console.print()
        
        # CONFIRMAR si es el activo correcto
        confirmacion = inquirer.select(
            message="¿Es este el activo correcto?",
            choices=[
                {'name': '✅ Sí, continuar con este activo', 'value': 'si'},
                {'name': '🔄 No, buscar otro activo', 'value': 'reintentar'},
                {'name': '❌ Cancelar y volver al menú', 'value': 'cancelar'}
            ]
        ).execute()
        
        if confirmacion == 'cancelar':
            return
        elif confirmacion == 'reintentar':
            continue  # Volver al principio del bucle
        
        # Si llegamos aquí, el usuario confirmó el activo
        # Ahora pedimos los datos de la operación
        break
    
    # Continuar con los datos de la operación
    clear_screen()
    mostrar_cabecera()
    
    nombre_default = precio_data['nombre'] if precio_data else isin
    
    console.print(Panel(
        f"[bold]📝 Datos de la operación[/bold]\n\n"
        f"[dim]Activo: {nombre_default}[/dim]\n"
        f"[dim]ISIN: {isin}[/dim]",
        border_style="blue"
    ))
    console.print()
    
    # Nombre (con valor por defecto del activo encontrado)
    nombre = inquirer.text(
        message="Nombre del activo:",
        default=nombre_default
    ).execute()
    
    # Cantidad
    cantidad_str = inquirer.text(
        message="Cantidad (participaciones/acciones):",
        validate=lambda x: x.replace('.', '').replace(',', '').isdigit() and float(x.replace(',', '.')) > 0,
        invalid_message="Introduce un número válido mayor que 0"
    ).execute()
    cantidad = float(cantidad_str.replace(',', '.'))
    
    # Precio de compra
    precio_str = inquirer.text(
        message="Precio de compra (por unidad, en €):",
        validate=lambda x: x.replace('.', '').replace(',', '').isdigit() and float(x.replace(',', '.')) > 0,
        invalid_message="Introduce un número válido mayor que 0"
    ).execute()
    precio_compra = float(precio_str.replace(',', '.'))
    
    # Fecha de compra
    fecha_default = datetime.now().strftime("%Y-%m-%d")
    fecha = inquirer.text(
        message="Fecha de compra (YYYY-MM-DD):",
        default=fecha_default
    ).execute()
    
    # Broker (opcional)
    broker = inquirer.text(
        message="Broker (opcional):",
        default=""
    ).execute()
    
    # Resumen final y confirmación
    console.print()
    coste_total = cantidad * precio_compra
    
    # Calcular beneficio/pérdida actual si tenemos precio
    beneficio_texto = ""
    if precio_data:
        valor_actual = cantidad * precio_data['precio']
        beneficio = valor_actual - coste_total
        rentabilidad = (beneficio / coste_total) * 100 if coste_total > 0 else 0
        color = "green" if beneficio >= 0 else "red"
        signo = "+" if beneficio >= 0 else ""
        beneficio_texto = (
            f"\n[bold]💹 Valor actual:[/bold] {valor_actual:,.2f} €\n"
            f"[bold]📊 Beneficio:[/bold] [{color}]{signo}{beneficio:,.2f} € ({signo}{rentabilidad:.2f}%)[/{color}]"
        )
    
    console.print(Panel(
        f"[bold]ISIN:[/bold] {isin}\n"
        f"[bold]Ticker:[/bold] {ticker or '(búsqueda por ISIN)'}\n"
        f"[bold]Nombre:[/bold] {nombre}\n"
        f"[bold]Cantidad:[/bold] {cantidad:,.4f}\n"
        f"[bold]Precio compra:[/bold] {precio_compra:,.2f} €\n"
        f"[bold]Coste total:[/bold] {coste_total:,.2f} €\n"
        f"[bold]Fecha:[/bold] {fecha}\n"
        f"[bold]Broker:[/bold] {broker or 'N/A'}"
        f"{beneficio_texto}",
        title="📋 Resumen de la posición",
        border_style="cyan"
    ))
    
    confirmar = inquirer.confirm(
        message="¿Añadir esta posición a la cartera?",
        default=True
    ).execute()
    
    if confirmar:
        posicion = Position(
            isin=isin,
            ticker=ticker,
            nombre=nombre,
            cantidad=cantidad,
            precio_compra=precio_compra,
            fecha_compra=fecha,
            broker=broker
        )
        portfolio.agregar_posicion(posicion)
        guardar_portfolio(portfolio)
        console.print("\n[green]✅ Posición añadida correctamente[/green]")
    else:
        console.print("\n[yellow]⚠ Operación cancelada[/yellow]")
    
    console.print()
    inquirer.select(message="", choices=["← Continuar"]).execute()


def ver_posiciones(portfolio: Portfolio):
    """Muestra la lista de posiciones para gestionar"""
    clear_screen()
    mostrar_cabecera()
    
    if not portfolio.posiciones:
        console.print(Panel(
            "[yellow]📭 No tienes posiciones en tu cartera[/yellow]",
            border_style="yellow"
        ))
        inquirer.select(message="", choices=["← Volver"]).execute()
        return
    
    # Crear lista de opciones
    opciones = []
    for pos in portfolio.posiciones:
        opciones.append({
            'name': f"{pos.nombre} ({pos.ticker}) - {pos.cantidad} uds @ {pos.precio_compra:.2f}€",
            'value': pos.id
        })
    
    opciones.append(Separator())
    opciones.append({'name': '← Volver al menú', 'value': 'volver'})
    
    seleccion = inquirer.select(
        message="Selecciona una posición:",
        choices=opciones
    ).execute()
    
    if seleccion == 'volver':
        return
    
    # Mostrar detalle de la posición
    posicion = portfolio.obtener_posicion(seleccion)
    if posicion:
        mostrar_detalle_posicion(portfolio, posicion)


def mostrar_detalle_posicion(portfolio: Portfolio, posicion: Position):
    """Muestra el detalle de una posición y permite gestionarla"""
    clear_screen()
    mostrar_cabecera()
    
    # Obtener precio actual (con fallback por ISIN)
    precio_data = price_fetcher.obtener_precio(posicion.ticker, posicion.isin)
    
    if precio_data:
        precio_actual = precio_data['precio']
        valor_actual = posicion.cantidad * precio_actual
        beneficio = valor_actual - posicion.coste_total
        rentabilidad = (beneficio / posicion.coste_total * 100) if posicion.coste_total > 0 else 0
        color = "green" if beneficio >= 0 else "red"
        signo = "+" if beneficio >= 0 else ""
        
        precio_text = (
            f"\n[bold]💹 Precio actual:[/bold] {precio_actual:,.2f} €\n"
            f"[bold]💰 Valor actual:[/bold] {valor_actual:,.2f} €\n"
            f"[bold]📊 Beneficio:[/bold] [{color}]{signo}{beneficio:,.2f} € ({signo}{rentabilidad:.2f}%)[/{color}]"
        )
    else:
        precio_text = "\n[yellow]⚠ No se pudo obtener el precio actual[/yellow]"
    
    console.print(Panel(
        f"[bold]📌 {posicion.nombre}[/bold]\n\n"
        f"[bold]ISIN:[/bold] {posicion.isin}\n"
        f"[bold]Ticker:[/bold] {posicion.ticker}\n"
        f"[bold]Cantidad:[/bold] {posicion.cantidad:,.2f}\n"
        f"[bold]Precio compra:[/bold] {posicion.precio_compra:,.2f} €\n"
        f"[bold]Coste total:[/bold] {posicion.coste_total:,.2f} €\n"
        f"[bold]Fecha compra:[/bold] {posicion.fecha_compra}\n"
        f"[bold]Broker:[/bold] {posicion.broker or 'N/A'}"
        f"{precio_text}",
        title="📋 Detalle de Posición",
        border_style="blue"
    ))
    console.print()
    
    accion = inquirer.select(
        message="¿Qué quieres hacer?",
        choices=[
            {'name': '🗑️  Eliminar posición', 'value': 'eliminar'},
            {'name': '📈 Ver gráfico histórico', 'value': 'grafico'},
            {'name': '← Volver', 'value': 'volver'}
        ]
    ).execute()
    
    if accion == 'eliminar':
        confirmar = inquirer.confirm(
            message="¿Seguro que quieres eliminar esta posición?",
            default=False
        ).execute()
        
        if confirmar:
            portfolio.eliminar_posicion(posicion.id)
            guardar_portfolio(portfolio)
            console.print("\n[green]✅ Posición eliminada[/green]")
            inquirer.select(message="", choices=["← Continuar"]).execute()
    
    elif accion == 'grafico':
        mostrar_grafico(posicion.ticker, posicion.nombre)


def mostrar_grafico(ticker: str, nombre: str):
    """Muestra un gráfico ASCII del histórico de precios"""
    clear_screen()
    mostrar_cabecera()
    
    try:
        import plotext as plt
        
        console.print(f"[bold]📈 Cargando histórico de {nombre}...[/bold]\n")
        
        historico = price_fetcher.obtener_historico(ticker, "6mo")
        
        if historico is None or historico.empty:
            console.print("[yellow]⚠ No hay datos históricos disponibles[/yellow]")
        else:
            # Preparar datos
            fechas = [d.strftime("%Y-%m-%d") for d in historico.index]
            precios = historico['Close'].tolist()
            
            # Crear gráfico
            plt.clear_figure()
            plt.plot(precios, label=ticker)
            plt.title(f"Histórico 6 meses - {nombre}")
            plt.xlabel("Tiempo")
            plt.ylabel("Precio (€)")
            plt.theme("dark")
            plt.plot_size(100, 25)
            plt.show()
            
    except ImportError:
        console.print("[yellow]⚠ Instala plotext para ver gráficos: pip install plotext[/yellow]")
    except Exception as e:
        console.print(f"[red]Error generando gráfico: {e}[/red]")
    
    console.print()
    inquirer.select(message="", choices=["← Volver"]).execute()


def menu_principal():
    """Menú principal de la aplicación"""
    portfolio = cargar_portfolio()
    
    while True:
        clear_screen()
        mostrar_cabecera()
        
        # Mostrar resumen rápido si hay posiciones
        if portfolio.posiciones:
            num_pos = len(portfolio.posiciones)
            console.print(f"[dim]📊 {num_pos} posición{'es' if num_pos > 1 else ''} en cartera[/dim]\n")
        
        opcion = inquirer.select(
            message="¿Qué quieres hacer?",
            choices=[
                {'name': '📊  Ver resumen de cartera', 'value': 'resumen'},
                {'name': '➕  Añadir nueva posición', 'value': 'agregar'},
                {'name': '📋  Gestionar posiciones', 'value': 'posiciones'},
                Separator(),
                {'name': '❌  Salir', 'value': 'salir'}
            ],
            pointer="▶"
        ).execute()
        
        if opcion == 'resumen':
            mostrar_resumen(portfolio)
        elif opcion == 'agregar':
            agregar_posicion(portfolio)
            portfolio = cargar_portfolio()  # Recargar
        elif opcion == 'posiciones':
            ver_posiciones(portfolio)
            portfolio = cargar_portfolio()  # Recargar por si se eliminó algo
        elif opcion == 'salir':
            clear_screen()
            console.print(Panel(
                "[bold blue]👋 ¡Hasta pronto![/bold blue]\n\n"
                "[dim]Tus datos se han guardado automáticamente.[/dim]",
                border_style="blue"
            ))
            break


if __name__ == "__main__":
    try:
        menu_principal()
    except KeyboardInterrupt:
        console.print("\n[yellow]Saliendo...[/yellow]")
        sys.exit(0)
