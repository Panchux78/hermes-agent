"""Opt-in accounting menu; existing Hermes commands and callbacks stay upstream.

The six workflows are the existing ContaBot implementations, not new parsers.
This module owns only navigation, authorization and their adapter bindings.
"""
from collections.abc import Mapping

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from plugins.platforms.telegram.menu_buttons import aligned_menu_label, menu_label

# page -> (title, parent page, parent label, [(icon, label, callback)])
PAGES = {
    "main": ("Hola, soy {name}, tu asistente de IA contable.\n¿Qué querés hacer hoy?", None, "", [
        ("🏛️", "Organismos fiscales", "om:organismos"), ("🏦", "Bancos", "om:bancos"),
        ("🧰", "Herramientas", "om:herramientas"), ("ℹ️", "Ayuda", "om:ayuda")]),
    "organismos": ("Organismos fiscales\nElegí el organismo con el que necesitás operar.", "main", "Menú principal", [
        ("🏛️", "ARCA", "om:arca"), ("💵", "AGIP", "om:agip"), ("🪙", "ARBA", "om:arba")]),
    "arca_consultar": ("ARCA · Consultar", "arca", "ARCA", [
        ("📥", "CSV de períodos presentados", "pi:descargar")]),
    "arca_preparar": ("ARCA · Preparar", "arca", "ARCA", [
        ("🧾", "Preparar período nuevo", "pi:generar")]),
    "agip_consultar": ("AGIP · Consultar", "agip", "AGIP", [
        ("🧾", "DDJJ de IIBB", "ad:start")]),
    "bancos": ("Bancos\nConvertí resúmenes bancarios compatibles a Excel. No convierte PDFs generales.",
        "main", "Menú principal", [("🏦", "Resumen bancario → Excel", "px:start"),
        ("📦", "Lote de resúmenes bancarios → Excel", "bx:start")]),
    "herramientas": ("Herramientas\nProtegé o desbloqueá archivos PDF. Estas opciones no generan Excel.",
        "main", "Menú principal", [("🔒", "Proteger PDF", "ps:protect:start"),
        ("🔓", "Desbloquear PDF", "ps:unlock:start")]),
    "ayuda": ("¿Con qué necesitás ayuda?", "main", "Menú principal", [
        ("ℹ️", "Qué hace {name}", "om:que_hace"), ("💬", "Hacer una consulta", "om:consulta")]),
    "que_hace": ("Qué hace {name}\nConsulta DDJJ de IIBB y Portal IVA; convierte resúmenes bancarios "
        "y lotes; protege y desbloquea PDFs. También conserva sus skills y comandos anteriores.",
        "ayuda", "Ayuda", []),
    "consulta": ("Escribí tu consulta en el chat. Los comandos y capacidades anteriores siguen disponibles.",
        "ayuda", "Ayuda", []),
    "admin": ("Administración\nPrepará una actualización y revisá sus cambios antes de confirmarla.",
        "main", "Menú principal", [("🧾", "Actualizar mapa de impuestos", "oa:arca:start"),
        ("🏦", "Actualizar bancos BCRA", "oa:bcra:start")]),
}
for authority in ("arca", "agip", "arba"):
    PAGES[authority] = (authority.upper() + "\nElegí qué necesitás hacer.", "organismos", "Organismos", [
        ("🔎", "Consultar", f"om:{authority}_consultar"),
        ("🧾", "Preparar", f"om:{authority}_preparar"),
        ("📤", "Presentar", f"om:{authority}_presentar")])
    for action, label in (("consultar", "consultas"), ("preparar", "preparación"), ("presentar", "presentaciones")):
        PAGES.setdefault(f"{authority}_{action}", (
            f"{authority.upper()} · {action.capitalize()}\nTodavía no hay funciones de {label} habilitadas.",
            authority, authority.upper(), []))


class OperationalMenu:
    def __init__(self, extra):
        settings = extra["operational_menu"]
        if (not isinstance(settings, Mapping) or set(settings) != {"name"}
                or not isinstance(settings["name"], str) or not 1 <= len(settings["name"]) <= 60
                or any(ord(c) < 32 for c in settings["name"])):
            raise ValueError("OPERATIONAL_MENU_CONFIG_INVALID")
        self.name = settings["name"]
        self.technical_user = str(extra.get("technical_menu_user_id", ""))
        from plugins.platforms.telegram.contabot_deployment import deployment_paths
        from plugins.platforms.telegram.pdf_xlsx_flow import PdfXlsxFlow
        from plugins.platforms.telegram.batch_pdf_xlsx_flow import BatchPdfXlsxFlow
        from plugins.platforms.telegram.pdf_security_flow import PdfSecurityFlow
        from plugins.platforms.telegram.agip_ddjj_flow import AgipDdjjFlow
        from plugins.platforms.telegram.portal_iva_flow import PortalIvaFlow
        from plugins.platforms.telegram.admin_maintenance_flow import AdminMaintenanceFlow
        paths = deployment_paths(extra)
        required = {"project_dir", "runtime_python", "portal_iva_executor", "arca_map"}
        if set(paths) != required:
            raise ValueError("OPERATIONAL_MENU_PATHS_REQUIRED")
        project, runtime = paths["project_dir"], paths["runtime_python"]
        self.flows = {
            "px": PdfXlsxFlow(router_project_dir=project, runtime_python=runtime),
            "bx": BatchPdfXlsxFlow(project_dir=project, runtime_python=runtime),
            "ps": PdfSecurityFlow(),
            "ad": AgipDdjjFlow(worker=project / "scripts/agip-ddjj-worker.py", runtime_python=runtime),
            "pi": PortalIvaFlow(executor=paths["portal_iva_executor"], runtime_python=runtime),
            "oa": AdminMaintenanceFlow(project_dir=project, runtime_python=runtime, arca_map=paths["arca_map"]),
        }

    def is_admin(self, user_id, chat_id, chat_type):
        return (self.technical_user.isdigit() and chat_type == "private"
                and str(user_id) == str(chat_id) == self.technical_user)

    def keyboard(self, page="main", *, admin=False):
        _, parent, parent_label, items = PAGES[page]
        items = list(items)
        if page == "main" and admin:
            items.append(("⚙️", "Administración", "om:admin"))
        alignment_page = "administracion" if page == "admin" else page
        rows = [[InlineKeyboardButton(
            aligned_menu_label(alignment_page, icon, label.format(name=self.name)), callback_data=callback)]
            for icon, label, callback in items]
        nav = []
        if parent:
            nav.append(InlineKeyboardButton(menu_label("‹", parent_label), callback_data=f"om:{parent}"))
        nav.append(InlineKeyboardButton(menu_label("✕", "Cerrar"), callback_data="om:close"))
        return InlineKeyboardMarkup(rows + [nav])

    async def open(self, message):
        await message.reply_text(
            f"☰  Menú de {self.name}",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton(menu_label("☰", "Menú"))]],
                resize_keyboard=True, is_persistent=True))
        await message.reply_text(PAGES["main"][0].format(name=self.name),
            reply_markup=self.keyboard(admin=self.is_admin(
                message.from_user.id, message.chat_id, message.chat.type)))

    async def callback(self, adapter, query, cb):
        prefix, _, page = query.data.partition(":")
        if prefix not in {"om", *self.flows}:
            return False
        if not await adapter._callback_authorized(query, cb, "No estás autorizado para usar esta opción."):
            return True
        user_id = str(query.from_user.id)
        if cb["chat_type"] != "private" or cb["chat_id"] is None:
            await query.answer("Usá estas opciones en el chat privado del bot.")
            return True
        admin = self.is_admin(user_id, cb["chat_id"], cb["chat_type"])
        if (prefix == "oa" or (prefix == "om" and page == "admin")) and not admin:
            await query.answer("Esta opción es exclusiva de Administración.")
            return True
        if prefix != "om":
            flow = self.flows[prefix]
            if prefix == "pi" and not flow.available():
                await query.answer("Portal IVA no está disponible en este equipo.")
                return True
            await flow.callback(adapter, query, query.data, cb["chat_id"], cb["thread_id"], user_id)
            return True
        if page == "close":
            await query.answer()
            await query.edit_message_reply_markup(reply_markup=None)
            return True
        if page not in PAGES:
            await query.answer("Esta opción ya no está disponible. Abrí Menú.")
            return True
        await query.answer()
        await query.edit_message_text(PAGES[page][0].format(name=self.name),
            reply_markup=self.keyboard(page, admin=admin))
        return True

    async def text(self, adapter, message):
        if getattr(message.chat, "type", None) != "private":
            return False
        if (message.text or "").strip() in {menu_label("☰", "Menú"), "☰ Menú", "Menú"}:
            await self.open(message)
            return True
        for prefix in ("ps", "ad", "pi"):
            if await self.flows[prefix].text(adapter, message):
                return True
        return False

    async def document(self, adapter, message):
        if getattr(message.chat, "type", None) != "private" or not message.document:
            return False
        for prefix in ("ps", "bx", "px"):
            if await self.flows[prefix].document(adapter, message):
                return True
        return False


def build_operational_menu(extra):
    if "operational_menu" not in extra:
        return None
    return OperationalMenu(extra)
