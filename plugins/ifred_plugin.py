import os, sys
from typing import List, Dict
import idaapi
import ida_name
import ida_kernwin
import ida_nalt
import ida_registry
import idautils 
from ifred.qt_bindings import *

from ifred.api import Action, cleanup_palettes, set_path_handler, show_palette
from ifred.utils import load_json

if idaapi.IDA_SDK_VERSION < 900:
    import ida_struct 
    import ida_enum 

# Platform-specific shortcuts
if sys.platform == "darwin":
    CMD_PALETTE_SHORTCUT = "Meta+Shift+P"
    NAME_PALETTE_SHORTCUT = "Meta+P"
else:
    CMD_PALETTE_SHORTCUT = "Ctrl+Shift+P"
    NAME_PALETTE_SHORTCUT = "Ctrl+P"

def get_blacklist() -> List[QRegularExpression]:
    """Get blacklisted patterns from config"""
    try:
        blacklist = load_json("config.json")["blacklist"]
        return [QRegularExpression(pattern) for pattern in blacklist if pattern]
    except:
        return []

def add_actions(result: List[Action], actions: List[str]):
    """Add IDA actions to result list"""
    blacklist = get_blacklist()
    remove_tilde = QRegularExpression("~(.*?)~")

    for item in actions:
        # Check blacklist
        if any(pattern.match(item).hasMatch() for pattern in blacklist):
            continue

        # Get action state
        ok, state = idaapi.get_action_state(item)
        if not ok or state > idaapi.AST_ENABLE:
            continue

        # Get action metadata
        label = idaapi.get_action_label(item)
        shortcut = idaapi.get_action_shortcut(item)

        if label:
            label = str(label).replace("~", "")
            result.append(Action(item, label, shortcut or ""))

def add_names(result: List[Action], names_count: int):
    """Add names from IDA to result list"""
    for i in range(names_count):
        ea = idaapi.get_nlist_ea(i)
        name = ida_name.get_demangled_name(ea, 0, 0, ida_name.GN_SHORT)
        if name:
            result.append(Action(hex(ea), name))

def get_actions() -> List[Action]:
    """Get all available IDA actions"""
    result = []
    actions = idaapi.get_registered_actions()

    # Add actions except blacklisted
    add_actions(result, actions)

    # Sort by name
    result.sort(key=lambda x: x.name.lower())

    return result

def get_nice_struc_name(tid: int) -> str:
    """Get readable structure name"""
    """
    tid is actually ordinal in IDA 9.0 above
    """
    if idaapi.IDA_SDK_VERSION < 900:
        name = ida_struct.get_struc_name(tid)
        if name:
            tif = idaapi.tinfo_t()
            if tif.get_named_type(idaapi.get_idati(), name):
                return str(tif)
        return name or ""
    else:
        ordinal = tid 
        tif = idaapi.tinfo_t()
        tif.get_numbered_type(idaapi.get_idati(), ordinal)
        return tif.dstr()


def add_structs(result: List[Action]):
    """Add structures to result list"""
    idx = ida_struct.get_first_struc_idx()
    while idx != idaapi.BADADDR:
        sid = ida_struct.get_struc_by_idx(idx)
        if sid != idaapi.BADADDR:
            name = get_nice_struc_name(sid)
            result.append(Action(f"struct:{sid}", name))
        idx = ida_struct.get_next_struc_idx(idx)

def add_enums(result: List[Action]):
    """Add enums to result list"""
    for i in range(ida_enum.get_enum_qty()):
        enum_id = ida_enum.getn_enum(i)
        if enum_id != idaapi.BADADDR:
            name = get_nice_struc_name(enum_id)
            result.append(Action(f"struct:{enum_id}", name))

def add_types(result: List[Action]):
    if idaapi.IDA_SDK_VERSION < 900:
        add_structs(result)
        add_enums(result)
    else:
        for ordinal in range(1, idaapi.get_ordinal_count()+1):
            name = idaapi.get_numbered_type_name(idaapi.get_idati(), ordinal)
            if name:
                result.append(Action(f"struct:{ordinal}", get_nice_struc_name(ordinal)))


class NamesManager:
    class IDBHooker(idaapi.IDB_Hooks):
        def __init__(self, mgr, _flags=0, _hkcb_flags=1):
            super().__init__(_flags, _hkcb_flags)
            self.mgr: "NamesManager" = mgr 
        
        def renamed(self, ea, new_name, local_name, old_name):
            self.mgr.rename(ea, new_name) 

        def allsegs_moved(self, info):
            self.mgr.rebase(info)

        def struc_renamed(self, struc, success):
            tid = struc.tid 
            self.mgr.update_struct(tid, get_nice_struc_name(tid))

        def struc_created(self, tid):
            self.mgr.update_struct(tid, get_nice_struc_name(tid))

        def enum_created(self, tid):
            self.mgr.update_struct(tid, get_nice_struc_name(tid))

        def enum_renamed(self, tid):
            self.mgr.update_struct(tid, get_nice_struc_name(tid))
    
    class IDBHooker900(idaapi.IDB_Hooks):
        def __init__(self, mgr, _flags=0, _hkcb_flags=1):
            super().__init__(_flags, _hkcb_flags)
            self.mgr = mgr 
        
        def renamed(self, ea, new_name, local_name, old_name):
            self.mgr.rename(ea, new_name) 

        def allsegs_moved(self, info):
            self.mgr.rebase(info)

        def local_types_changed(self, ltc, ordinal, name):
            if ltc in [idaapi.LTC_ADDED, idaapi.LTC_ALIASED, idaapi.LTC_EDITED]:
                # print(f"local_types_changed. ordinal = {ordinal}, name = {name}")
                self.mgr.update_struct(ordinal, name)
    
    class IDPHooker(idaapi.IDP_Hooks):
        def __init__(self, mgr, *args):
            super().__init__(*args)
            self.mgr: "NamesManager" = mgr 

        def ev_term(self):
            self.mgr.clear()

    def __init__(self):
        self.address_to_name = {}
        self.address_to_struct = {}
        self.result = []
        if idaapi.IDA_SDK_VERSION < 900:
            self.idb_hooker = NamesManager.IDBHooker(self)
        else:
            self.idb_hooker = NamesManager.IDBHooker900(self)
        self.idp_hooker = NamesManager.IDPHooker(self)

        self.idb_hooker.hook()
        self.idp_hooker.hook()
        #TODO, when to unhook?

    def init(self, names):
        self.address_to_name.clear()
        self.address_to_struct.clear()

        for index, action in enumerate(names):
            if action.id.startswith("struct:"):
                struct_id = int(action.id.split(':')[1])
                self.address_to_struct[struct_id] = index
            else:
                addr = int(action.id, 16)
                self.address_to_name[addr] = index

    def rename(self, address, name):
        if address in self.address_to_name:
            demangled = ida_name.get_demangled_name(address, 0, 0, ida_name.GN_SHORT)
            action = self.result[self.address_to_name[address]]
            action.name = demangled
            action.id = hex(address)
        elif self.result:  # Only if initialized
            demangled = ida_name.get_demangled_name(address, 0, 0, ida_name.GN_SHORT)
            self.result.append(Action(hex(address), demangled))
            self.address_to_name[address] = len(self.result) - 1

    def rebase(self, infos):
        moves = []
        for seg in infos:
            for key in list(self.address_to_name.keys()):
                if seg._from <= key and key < seg._from + seg.size:
                    moves.append((key, key + seg.to - seg._from))

        for old_ea, new_ea in moves:
            index = self.address_to_name[old_ea]
            action = self.result[index]
            del self.address_to_name[old_ea]
            action.id = hex(new_ea)
            self.address_to_name[new_ea] = index

    def update_struct(self, id, name):
        if not self.result:  # Not initialized yet
            return

        if id in self.address_to_struct:
            action = self.result[self.address_to_struct[id]]
            action.name = name
        else:
            self.result.append(Action(f"struct:{id}", name))
            self.address_to_struct[id] = len(self.result) - 1

    def clear(self):
        self.result.clear()
        self.address_to_name.clear()
        self.address_to_struct.clear()

    def get(self, clear=False):
        names_count = idaapi.get_nlist_size()
        # structs_count = ida_struct.get_struc_qty()

        if self.result and not clear:
            return self.result

        self.result = []

        # Add names
        add_names(self.result, names_count)

        # Add types
        add_types(self.result)

        self.init(self.result)
        return self.result

   

def get_names(clear=False):
    global _names_manager
    if not '_names_manager' in globals():
        _names_manager = NamesManager()
    return _names_manager.get(clear)

class CommandPaletteHandler(idaapi.action_handler_t):
    def activate(self, ctx):
        shortcut = idaapi.get_action_shortcut(ctx.action)
        if shortcut:
            shortcut = shortcut.replace("-", "+")

        show_palette("command palette", "Enter action or option name...",
                    get_actions(), shortcut, lambda action: ida_kernwin.process_ui_action(action.id))
        return 1

    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS

class NamePaletteHandler(idaapi.action_handler_t):
    def activate(self, ctx):
        shortcut = idaapi.get_action_shortcut(ctx.action)
        if shortcut:
            shortcut = shortcut.replace("-", "+")

        def callback(action):
            if action.id.startswith("struct:"):
                tid = int(action.id[7:])
                if idaapi.IDA_SDK_VERSION < 900:
                    if ida_enum.get_enum_idx(tid) == idaapi.BADADDR:
                        ida_kernwin.open_structs_window(tid)
                    else:
                        ida_kernwin.open_enums_window(tid)
                else:
                    ordinal = tid 
                    tif = idaapi.tinfo_t()
                    tif.get_numbered_type(None, ordinal)
                    if tif is not None:
                        ida_kernwin.open_til_view_window(tif)
                    else:
                        print(f"[palatte] Something error happend, no corespoding structs/enums with ordinal {ordinal}")

            else:
                address = int(action.id, 16)
                ida_kernwin.jumpto(address)
                name = ida_name.get_ea_name(address)
                ida_registry.reg_update_strlist("History\\$", name, 32)
            return True

        show_palette(f"name palette{ida_nalt.get_input_file_path()}",
                    "Enter symbol name...",
                    get_names(), shortcut, callback)
        return 1

    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS

# Register command palette action
command_palette_handler = CommandPaletteHandler()
idaapi.register_action(
    idaapi.action_desc_t(
        "ifred:command_palette",
        "ifred command palette",
        command_palette_handler,
        CMD_PALETTE_SHORTCUT,
        "command palette"
    )
)

# Register name palette action
name_palette_handler = NamePaletteHandler()
idaapi.register_action(
    idaapi.action_desc_t(
        "ifred:name_palette",
        "ifred name palette",
        name_palette_handler,
        NAME_PALETTE_SHORTCUT,
        "name palette"
    )
)

def ida_plugin_path(filename):
    """Get plugin path for given filename"""
    plugin_path = os.path.join(idaapi.get_user_idadir(), "plugins", "ifred/res")
    os.makedirs(plugin_path, exist_ok=True)
    return os.path.join(plugin_path, filename)

class IfredPlugin(idaapi.plugin_t):
    flags = idaapi.PLUGIN_FIX | idaapi.PLUGIN_HIDE
    comment = "ifred"
    help = "IDA palette"
    wanted_name = "ifred"
    wanted_hotkey = ""

    def init(self):
        if not idaapi.is_idaq():
            return idaapi.PLUGIN_SKIP

        print("loading palettes...")

        # Set path handler
        set_path_handler(ida_plugin_path)

        # Check if default shortcut needs to be updated
        shortcut = idaapi.get_action_shortcut("CommandPalette")
        shortcut2 = idaapi.get_action_shortcut("ifred:command_palette")
        if shortcut == "Ctrl-Shift-P" and shortcut == shortcut2:
            idaapi.update_action_shortcut("CommandPalette", "")

        return idaapi.PLUGIN_KEEP

    def run(self, arg):
        return True

    def term(self):
        cleanup_palettes()

def PLUGIN_ENTRY():
    return IfredPlugin()
