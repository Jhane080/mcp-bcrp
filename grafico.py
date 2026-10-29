"""Dibuja grafico.png: cuántas de las 10 preguntas acertó Claude en cada modo.

    uv run grafico.py
"""
import matplotlib.pyplot as plt
import pandas as pd

resultados = pd.read_csv("resultados.csv")

# una fila por modo, con el conteo de cada resultado; fill_value=0 pone ceros donde no hubo casos
conteo = resultados.groupby("modo").resultado.value_counts().unstack(fill_value=0)
segundos = resultados.groupby("modo").segundos.mean()

# de menos a más información disponible, para que la barra crezca hacia abajo
orden = ["memoria", "web", "mcp"]
nombres = ["Sin herramientas", "Búsqueda web", "MCP del BCRP"]
aciertos = [conteo.loc[modo, "acierta"] for modo in orden]

figura, eje = plt.subplots(figsize=(7, 3), dpi=150)
eje.barh(nombres, aciertos, color="#2a78d6", height=0.5)
eje.invert_yaxis()   # barh dibuja el primero abajo; lo queremos arriba
eje.set_xlim(0, 10)
eje.set_xlabel("Preguntas acertadas (de 10)")

# la etiqueta dice también cuántas veces dijo «no sé» y cuánto tardó, que el largo de la barra no muestra
for fila, modo in enumerate(orden):
    no_sabe = conteo.loc[modo].get("no sabe", 0)
    texto = f"{aciertos[fila]} de 10 · {no_sabe} «no sé» · {segundos[modo]:.0f} s"
    eje.text(aciertos[fila] + 0.15, fila, texto, va="center", fontsize=9, color="#52514e")

eje.set_title("Claude Haiku y las cifras del BCRP: ninguna de memoria, todas con el MCP",
              loc="left", fontsize=10)
eje.spines[["top", "right"]].set_visible(False)
figura.tight_layout()
figura.savefig("grafico.png")
