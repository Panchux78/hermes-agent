"""#191: qué botón usar para comprobar una clave.

«Verificá la clave en Accesos» se entendía como «revisá que esté bien escrita»:
la usuaria borró y volvió a cargar la clave y nunca se comprobó. El aviso dice
ahora dónde está y qué botón tocar.
"""


def como_comprobar(entidad: str) -> str:
    return (f"Para comprobarla: en el panel, abrí la ficha del cliente, andá a Accesos fiscales y tocá «Comprobar» en {entidad}. "
            "Cuando diga «Comprobada», volvé a pedirlo. Volver a cargar la clave no la comprueba.")
