"""Servidor MCP del BCRP. Empiecen aquí: buscar_series ya funciona; completen los TODO hasta que pasen las pruebas.

    uv run pytest                                           # el contrato: las 8 pruebas deben pasar
    uv run server.py                                        # el servidor, por stdio
    claude mcp add --scope project bcrp -- uv run server.py # conectarlo a Claude Code

Las cuentas las hace Python, no el modelo: así cada cifra sale de una llamada que se puede repetir.
"""
import re
import time
import unicodedata
from functools import cache
from pathlib import Path

import httpx
import pandas as pd
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

AQUI = Path(__file__).parent
CATALOGO = AQUI / "datos" / "catalogo_bcrp.csv"
URL_CATALOGO = "https://estadisticas.bcrp.gob.pe/estadisticas/series/metadata"
API = "https://estadisticas.bcrp.gob.pe/estadisticas/series/api"
SOLO_LECTURA = {"readOnlyHint": True, "openWorldHint": True}

mcp = FastMCP("bcrp", instructions=(
    "Series oficiales del Banco Central de Reserva del Perú (BCRP). Primero busca el código con buscar_series; "
    "luego usa resumen para variaciones, promedios y extremos, y serie para los datos. No calcules a mano: "
    "cita siempre el código de la serie y el periodo."))


def _sin_tildes(s):
    return unicodedata.normalize("NFD", str(s).lower()).encode("ascii", "ignore").decode()


@cache
def catalogo():
    """El catálogo oficial: 17 mil series en unos 7 MB. Se baja una vez y queda en datos/."""
    if not CATALOGO.exists():
        CATALOGO.parent.mkdir(exist_ok=True)
        r = httpx.get(URL_CATALOGO, timeout=120)
        r.raise_for_status()
        CATALOGO.write_text(r.content.decode("latin-1"), encoding="utf-8")
    df = pd.read_csv(CATALOGO, sep=";", dtype=str).fillna("")
    df = df.rename(columns={"Código de serie": "codigo", "Categoría de serie": "categoria", "Grupo de serie": "grupo",
                            "Nombre de serie": "nombre", "Frecuencia": "frecuencia",
                            "Fecha de actualización": "actualizada", "Fecha de inicio": "inicio"})
    df["texto"] = (df.categoria + " " + df.grupo + " " + df.nombre).map(_sin_tildes)
    return df


def _ficha(codigo):
    fila = catalogo().query("codigo == @codigo")
    if fila.empty:
        raise ToolError(f"No existe el código de serie {codigo!r}. Búsquenlo con buscar_series.")
    f = fila.iloc[0]
    return {"codigo": f.codigo, "nombre": f"{f.grupo} - {f.nombre}", "categoria": f.categoria,
            "frecuencia": f.frecuencia, "inicio": f.inicio, "actualizada": f.actualizada}


@mcp.tool(annotations=SOLO_LECTURA)
def buscar_series(texto: str, frecuencia: str | None = None, limite: int = 10) -> list[dict]:
    """Busca series del BCRP por palabras: todas deben aparecer en la categoría, el grupo o el nombre.

    Ejemplos: "ipc 12 meses", "tipo de cambio interbancario venta", "reservas internacionales netas",
    "tasa de referencia", "cobre precio". frecuencia: "Diaria", "Mensual", "Trimestral" o "Anual".
    Devuelve el código, el nombre completo, la frecuencia y desde cuándo hay datos. Si no hay resultados,
    prueben con menos palabras.
    """
    df = catalogo()
    m = pd.Series(True, index=df.index)
    for palabra in _sin_tildes(texto).split():
        m &= df.texto.str.contains(re.escape(palabra))
    if frecuencia:
        m &= df.frecuencia.str.lower() == frecuencia.lower()
    # primero las series vigentes y, entre ellas, las de nombre más corto (las más generales)
    sel = df[m]
    orden = sel.assign(vieja=sel.actualizada < "2025", largo=(sel.grupo + sel.nombre).str.len())
    return [_ficha(c) for c in orden.sort_values(["vieja", "largo"]).head(limite).codigo]


# Los meses mensuales llegan como "Sep.2026" y los diarios como "30.Set.26": aceptamos las dos formas.
MESES = {"ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8, "sep": 9, "set": 9,
         "oct": 10, "nov": 11, "dic": 12}


def _fecha_api(fecha, frecuencia, al_final):
    """Pasa "2025", "2025-01", "2025-01-15" o "2025-T1" al formato que pide la API para esa frecuencia.

    al_final=True toma el último día del periodo (para «hasta»): "2025-03" en una serie diaria llega al 31 de marzo.
    """
    periodo = pd.Period(fecha.upper().replace("-T", "Q"))
    dia = periodo.end_time if al_final else periodo.start_time
    if frecuencia == "Diaria":
        return dia.strftime("%Y-%m-%d")
    if frecuencia == "Mensual":
        return f"{dia.year}-{dia.month}"
    if frecuencia == "Trimestral":
        return f"{dia.year}-{dia.quarter}"
    return str(dia.year)


def _anio(dos_digitos):
    # los diarios y trimestrales traen el año con dos dígitos; las series diarias empiezan en los noventa
    return ("19" if dos_digitos > "50" else "20") + dos_digitos


def _periodo(nombre):
    """ "Ene.2025" -> "2025-01", "01.Oct.26" -> "2026-10-01", "T1.25" -> "2025-T1", "2025" -> "2025"."""
    partes = nombre.split(".")
    if len(partes) == 1:
        return nombre
    if len(partes) == 3:
        dia, mes, anio = partes
        return f"{_anio(anio)}-{MESES[mes.lower()]:02d}-{dia}"
    a, b = partes
    if a.startswith("T"):
        return f"{_anio(b)}-{a}"
    return f"{b}-{MESES[a.lower()]:02d}"


@cache
def _descargar(url):
    """Pide la URL a la API. Cuando la apuran, responde una página HTML en vez de JSON: esperamos y reintentamos."""
    for intento in range(4):
        r = httpx.get(url, timeout=60)
        try:
            return r.json()
        except ValueError:
            time.sleep(3 * (intento + 1))
    raise ToolError(f"El BCRP no respondió con datos para {url}. Esperen unos segundos y reintenten.")


@mcp.tool(annotations=SOLO_LECTURA)
def serie(codigo: str, desde: str, hasta: str) -> dict:
    """Datos de una serie del BCRP entre dos fechas, ambas incluidas. Úsenla cuando necesiten los valores uno por uno.

    Fechas: "2025" (anual), "2025-01" (mensual), "2025-T1" (trimestral) o "2025-01-15" (diaria); sirve cualquier
    formato para cualquier serie: "2026-10" en una serie diaria trae todo octubre. Ejemplo: codigo="PN01273PM",
    desde="2025-01", hasta="2025-12". Para variaciones, promedios o extremos usen resumen, no cuenten a mano.
    Devuelve el nombre, la frecuencia, los datos como [{"periodo", "valor"}] y la URL de la fuente.
    """
    ficha = _ficha(codigo)
    frecuencia = ficha["frecuencia"]
    url = f"{API}/{codigo}/json/{_fecha_api(desde, frecuencia, False)}/{_fecha_api(hasta, frecuencia, True)}"
    respuesta = _descargar(url)
    datos = []
    for p in respuesta.get("periods", []):
        valor = p["values"][0]
        if valor != "n.d.":
            datos.append({"periodo": _periodo(p["name"]), "valor": round(float(valor), 4)})
    return {"codigo": codigo, "nombre": ficha["nombre"], "frecuencia": frecuencia, "datos": datos, "fuente": url}


@mcp.tool(annotations=SOLO_LECTURA)
def resumen(codigo: str, desde: str, hasta: str) -> dict:
    """Las cuentas de una serie del BCRP entre dos fechas: úsenla siempre que pregunten cuánto subió, bajó o promedió.

    Mismas fechas que serie. Ejemplo: codigo="PN01273PM", desde="2025-12", hasta="2026-09".
    Devuelve primero y ultimo (con su periodo), cambio (ultimo - primero, en la unidad de la serie: si la serie ya
    está en %, son puntos porcentuales), variacion_pct (cambio relativo en %), promedio, maximo y minimo con su
    periodo, y la URL de la fuente.
    """
    s = serie(codigo, desde, hasta)
    datos = s["datos"]
    if not datos:
        raise ToolError(f"La serie {codigo} no tiene datos entre {desde} y {hasta}. Revisen su fecha de inicio.")
    tabla = pd.DataFrame(datos)
    primero = datos[0]
    ultimo = datos[-1]
    cambio = ultimo["valor"] - primero["valor"]
    return {"codigo": codigo, "nombre": s["nombre"], "frecuencia": s["frecuencia"], "n": len(datos),
            "primero": primero, "ultimo": ultimo, "cambio": round(cambio, 4),
            "variacion_pct": round(100 * cambio / primero["valor"], 2) if primero["valor"] else None,
            "promedio": round(tabla.valor.mean(), 4),
            "maximo": datos[tabla.valor.idxmax()], "minimo": datos[tabla.valor.idxmin()],
            "fuente": s["fuente"]}


BANCO_MUNDIAL = "https://api.worldbank.org/v2/country/{paises}/indicator/{indicador}?format=json&date={desde}:{hasta}"


@mcp.tool(annotations=SOLO_LECTURA)
def comparar_paises(indicador: str = "NY.GDP.MKTP.KD.ZG", paises: list[str] | None = None,
                    desde: int = 2015, hasta: int = 2025) -> dict:
    """Compara al Perú con otros países con datos anuales del Banco Mundial. Úsenla cuando la pregunta compare
    países (el BCRP solo tiene datos del Perú) o pida el contexto regional de una cifra.

    indicador: código del Banco Mundial. Los más usados: "NY.GDP.MKTP.KD.ZG" (crecimiento del PBI real, %),
    "FP.CPI.TOTL.ZG" (inflación anual, %), "SL.UEM.TOTL.ZS" (desempleo, %), "NY.GDP.PCAP.CD" (PBI per cápita, US$),
    "SI.POV.DDAY" (pobreza extrema, %). paises: códigos ISO de tres letras; por defecto ["PER", "CHL", "COL", "MEX"].
    Ejemplo: indicador="FP.CPI.TOTL.ZG", paises=["PER", "BOL"], desde=2020, hasta=2025.
    Devuelve el nombre del indicador, los datos como [{"pais", "anio", "valor"}] y la URL de la fuente.
    """
    if paises is None:
        paises = ["PER", "CHL", "COL", "MEX"]
    url = BANCO_MUNDIAL.format(paises=";".join(paises), indicador=indicador, desde=desde, hasta=hasta)
    respuesta = httpx.get(url + "&per_page=1000", timeout=60).json()
    # si el indicador no existe, el Banco Mundial responde una sola pieza con el mensaje de error
    if len(respuesta) < 2 or respuesta[1] is None:
        raise ToolError(f"El Banco Mundial no tiene datos de {indicador!r} para {paises} entre {desde} y {hasta}.")
    datos = []
    for fila in respuesta[1]:
        if fila["value"] is not None:
            datos.append({"pais": fila["country"]["value"], "anio": int(fila["date"]), "valor": round(fila["value"], 4)})
    datos.sort(key=lambda d: (d["anio"], d["pais"]))
    return {"indicador": respuesta[1][0]["indicator"]["value"], "datos": datos, "fuente": url}


@mcp.resource("bcrp://serie/{codigo}")
def ficha(codigo: str) -> dict:
    """Ficha de una serie del BCRP: nombre, categoría, frecuencia, desde cuándo hay datos y última actualización."""
    return _ficha(codigo)


@mcp.prompt
def informe(tema: str) -> str:
    """Informe breve sobre un tema económico del Perú, con cada cifra verificable en el BCRP."""
    return (f"Escribe un informe de 5 viñetas sobre «{tema}» en el Perú. Busca las series con buscar_series y saca "
            "cada cifra de resumen o serie, sin calcular a mano. En cada viñeta cita el código de la serie y el "
            "periodo. Termina con una línea que diga qué no se puede concluir con estos datos.")


if __name__ == "__main__":
    mcp.run()
