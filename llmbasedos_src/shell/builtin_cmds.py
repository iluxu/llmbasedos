# llmbasedos_pkg/shell/builtin_cmds.py
import os
import json # Pour parser les arguments optionnels en JSON
import sys # Pour getattr(sys.modules[__name__])
from pathlib import Path
# shlex est utilisé par luca.py avant d'appeler ces commandes, pas besoin ici.
from typing import List, Any, Dict, Optional # Retrait de Callable, Awaitable car plus directement utilisés ici

from rich.console import Console # Pour le typage, bien que l'instance vienne de ShellApp
from rich.table import Table
from rich.text import Text
from rich.syntax import Syntax
from datetime import datetime # Pour formater les dates

# Import ShellApp type pour l'annotation de type (évite l'import circulaire au runtime)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .luca import ShellApp # Utilisé pour l'annotation de type de 'app'

# Shell utils pour la logique factorisée (comme le streaming LLM)
from .shell_utils import stream_llm_chat_to_console

BUILTIN_COMMAND_LIST = [
    "exit", "quit", "help", "connect", "cd", "pwd", 
    "ls", "dir", "cat", "rm", "licence", "llm"
]

# --- Commandes Built-in ---
# Signature : async def cmd_nom(args_list: List[str], app: 'ShellApp')

async def cmd_exit(args_list: List[str], app: 'ShellApp'):
    """Exits the luca-shell."""
    app.console.print("Exiting luca-shell...")
    raise EOFError # Signale à prompt_toolkit de quitter

async def cmd_quit(args_list: List[str], app: 'ShellApp'):
    """Alias for exit."""
    await cmd_exit(args_list, app)

async def cmd_help(args_list: List[str], app: 'ShellApp'):
    """Shows available commands or help for a specific command."""
    console = app.console
    if not args_list:
        console.print("[bold]Available luca-shell built-in commands:[/bold]")
        tbl = Table(show_header=True, header_style="bold magenta", title="Built-in Commands")
        tbl.add_column("Command", style="dim", width=15)
        tbl.add_column("Description")
        
        for cmd_name_str in BUILTIN_COMMAND_LIST:
            handler_func = getattr(sys.modules[__name__], f"cmd_{cmd_name_str}", None)
            docstring = handler_func.__doc__.strip().splitlines()[0] if handler_func and handler_func.__doc__ else "No description available."
            tbl.add_row(cmd_name_str, docstring)
        console.print(tbl)
        
        available_mcp_cmds = sorted(list(app.available_mcp_commands))
        if available_mcp_cmds:
            console.print(f"\n[bold]Available MCP commands ({len(available_mcp_cmds)}):[/bold]")
            # Afficher par groupe de N pour la lisibilité
            for i in range(0, len(available_mcp_cmds), 5):
                 console.print("  " + ", ".join(available_mcp_cmds[i:i+5]))
        else:
            console.print("\n[yellow]No MCP commands currently discovered. Try 'connect' or check gateway.[/yellow]")
            
        console.print("\nType 'help <builtin_command>' for details on built-ins.")
        console.print("For MCP methods, use 'mcp.listCapabilities' or refer to protocol docs.")
    else:
        cmd_to_help_str = args_list[0]
        handler_func = getattr(sys.modules[__name__], f"cmd_{cmd_to_help_str}", None)
        if handler_func and handler_func.__doc__:
            console.print(f"[bold]Help for built-in '{cmd_to_help_str}':[/bold]\n{handler_func.__doc__.strip()}")
        else:
            # Chercher dans les commandes MCP si ce n'est pas un builtin
            # Ceci nécessiterait d'avoir les descriptions des MCP methods, obtenues via mcp.listCapabilities
            console.print(f"No help found for built-in '{cmd_to_help_str}'. If it's an MCP command, try 'mcp.listCapabilities'.")

async def cmd_connect(args_list: List[str], app: 'ShellApp'):
    """Attempts to (re)connect to the MCP gateway."""
    app.console.print("Attempting to (re)connect to gateway...")
    if await app.ensure_connection(force_reconnect=True):
        app.console.print("[green]Successfully connected/reconnected to MCP Gateway.[/green]")
        app.console.print(f"Type 'mcp.hello' or 'mcp.listCapabilities' to see available remote commands.")
    else:
        app.console.print("[prompt.error]Failed to connect. Check gateway status and URL configured in shell.[/prompt.error]")

async def cmd_cd(args_list: List[str], app: 'ShellApp'):
    """Changes the current working directory. Usage: cd <path>"""
    console = app.console
    if not args_list:
        target_path_str = os.path.expanduser("~") # cd sans argument va au home
    else:
        target_path_str = args_list[0]
    
    current_cwd = app.get_cwd()
    # Gérer `~` et les chemins relatifs/absolus
    expanded_path_str = os.path.expanduser(target_path_str)
    
    new_path: Path
    if os.path.isabs(expanded_path_str):
        new_path = Path(expanded_path_str).resolve()
    else:
        new_path = (current_cwd / expanded_path_str).resolve()

    # Vérifier si le chemin est un répertoire valide via mcp.fs.list
    response = await app.send_mcp_request(None, "mcp.fs.list", [str(new_path)])
    if response and "result" in response: # Si la commande réussit, c'est un répertoire accessible
        app.set_cwd(new_path)
        # Le prompt se mettra à jour automatiquement
    elif response and "error" in response:
        console.print(f"[prompt.error]cd: {response['error']['message']} (path: '{new_path}')[/prompt.error]")
    else:
        console.print(f"[prompt.error]cd: Error verifying path '{new_path}'. No or invalid response from gateway.[/prompt.error]")

async def cmd_pwd(args_list: List[str], app: 'ShellApp'):
    """Prints the current working directory."""
    app.console.print(str(app.get_cwd()))

async def _rich_format_mcp_fs_list(console: Console, result: List[Dict[str,Any]], request_path: str):
    """Formats mcp.fs.list output with Rich Table."""
    table = Table(show_header=True, header_style="bold cyan", title=f"Contents of {request_path if request_path else 'directory'}")
    table.add_column("Type", width=10); table.add_column("Size", justify="right", width=10)
    table.add_column("Modified", width=20); table.add_column("Name")
    
    for item in sorted(result, key=lambda x: (x.get('type') != 'directory', str(x.get('name','')).lower())):
        item_type = item.get("type", "other")
        color = "blue" if item_type == "directory" else "green" if item_type == "file" else "magenta" if item_type == "symlink" else "bright_black"
        
        size_val = item.get("size", -1)
        size_str = ""
        if item_type == "directory": size_str = "[DIR]"
        elif isinstance(size_val, int) and size_val >= 0:
            num = float(size_val); units = ["B", "KB", "MB", "GB", "TB"]; i = 0
            while num >= 1024 and i < len(units) - 1: num /= 1024.0; i += 1
            size_str = f"{num:.1f}{units[i]}" if i > 0 else f"{int(num)}{units[i]}"
        else: size_str = "N/A"
            
        mod_iso = item.get("modified_at", "")
        mod_display = datetime.fromisoformat(mod_iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M") if mod_iso else "N/A"
        
        table.add_row(Text(item_type, style=color), size_str, mod_display, Text(item.get("name", "?"), style=color))
    console.print(table)

async def _rich_format_mcp_fs_read(console: Console, result: Dict[str,Any]):
    """Formats mcp.fs.read output with Rich Syntax or plain."""
    content = result.get("content", "")
    encoding = result.get("encoding")
    mime_type = result.get("mime_type", "application/octet-stream")
    
    if encoding == "text":
        lexer = "text" # Default lexer
        simple_mime = mime_type.split('/')[-1].split('+')[0].lower()
        if simple_mime in ["json", "xml", "python", "markdown", "html", "css", "javascript", "yaml", "x-sh", "toml", "text"]:
            lexer = simple_mime
            if lexer in ["vnd.yaml", "x-yaml"]: lexer = "yaml"
            if lexer in ["x-python"]: lexer = "python"
            if lexer in ["x-shellscript", "x-sh"]: lexer = "bash"
        
        console.print(Syntax(content, lexer, theme="native", line_numbers=True, word_wrap=True, background_color="default"))
    elif encoding == "base64":
        console.print(f"[yellow]File content (base64 encoded, MIME: {mime_type}):[/yellow]")
        console.print(content[:500] + ("..." if len(content) > 500 else ""))
    else: # Fallback, ou si encoding est inconnu
        console.print(str(result))

async def _fs_command_proxy(mcp_method: str, args_list: List[str], app: 'ShellApp'):
    """Helper to proxy FS commands, resolve paths, and format output."""
    console = app.console
    if not args_list:
        console.print(f"[prompt.error]Usage: {mcp_method.split('.')[-1]} <path> [other_args...][/prompt.error]")
        return

    path_str_arg = args_list[0]
    # Résoudre le chemin (gère ~, relatif, absolu)
    expanded_path_str = os.path.expanduser(path_str_arg) if path_str_arg.startswith('~') else path_str_arg
    abs_path_obj = (app.get_cwd() / expanded_path_str).resolve() if not os.path.isabs(expanded_path_str) else Path(expanded_path_str).resolve()
    
    mcp_params: List[Any] = [str(abs_path_obj)]
    # Ajouter les arguments restants, en essayant de les parser en JSON si possible
    if len(args_list) > 1:
        for arg_s in args_list[1:]:
            try: mcp_params.append(json.loads(arg_s))
            except json.JSONDecodeError: mcp_params.append(arg_s)

    response = await app.send_mcp_request(None, mcp_method, mcp_params)

    if not response: console.print("[prompt.error]No response or connection failed.[/prompt.error]"); return
    if "error" in response:
        err = response["error"]
        console.print(f"[prompt.error]MCP Error (Code {err.get('code')}): {err.get('message')}[/prompt.error]")
        if "data" in err: console.print(Syntax(json.dumps(err['data'], indent=2), "json", theme="native", background_color="default"))
    elif "result" in response:
        if mcp_method == "mcp.fs.list":
            await _rich_format_mcp_fs_list(console, response["result"], str(abs_path_obj))
        elif mcp_method == "mcp.fs.read":
            await _rich_format_mcp_fs_read(console, response["result"])
        elif isinstance(response["result"], (dict, list)):
            console.print(Syntax(json.dumps(response["result"], indent=2), "json", theme="native", line_numbers=True, background_color="default"))
        else:
            console.print(str(response["result"]))
    else: # Réponse inconnue
        console.print(Syntax(json.dumps(response, indent=2), "json", theme="native", background_color="default"))

async def cmd_ls(args_list: List[str], app: 'ShellApp'):
    """Lists files and directories. Usage: ls [path]"""
    path_arg_str = args_list[0] if args_list else "."
    
    expanded_path_str = os.path.expanduser(path_arg_str) if path_arg_str.startswith('~') else path_arg_str
    abs_path_obj = (app.get_cwd() / expanded_path_str).resolve() if not os.path.isabs(expanded_path_str) else Path(expanded_path_str).resolve()
    
    response = await app.send_mcp_request(None, "mcp.fs.list", [str(abs_path_obj)])
    # Appeler la méthode de formatage de ShellApp
    await app._format_and_print_mcp_response("mcp.fs.list", response, request_path_for_ls=str(abs_path_obj))

async def cmd_dir(args_list: List[str], app: 'ShellApp'):
    """Alias for ls."""
    await cmd_ls(args_list, app)

async def cmd_cat(args_list: List[str], app: 'ShellApp'):
    """Displays file content. Usage: cat <path> [text|base64]"""
    if not args_list: app.console.print("[prompt.error]Usage: cat <path> [text|base64][/prompt.error]"); return
    # Passe tous les arguments à _fs_command_proxy, il prendra le premier comme chemin
    # et les suivants comme paramètres pour mcp.fs.read
    await _fs_command_proxy("mcp.fs.read", args_list, app)

async def cmd_rm(args_list: List[str], app: 'ShellApp'):
    """Deletes a file or directory. Usage: rm <path> [-r|--recursive] [--force|-f]"""
    console = app.console
    if not args_list:
        console.print("[prompt.error]Usage: rm <path> [-r|--recursive] [--force|-f][/prompt.error]"); return
    
    path_to_delete_str = args_list[0]
    recursive_flag = any(flag in args_list for flag in ["-r", "--recursive"])
    force_flag = any(flag in args_list for flag in ["-f", "--force"])

    if not force_flag:
        confirm_msg = f"Delete '{path_to_delete_str}'{' recursively' if recursive_flag else ''}? This is permanent. "
        console.print(f"[yellow]{confirm_msg}Add --force or -f to confirm. Skipping for now.[/yellow]")
        return

    # Résoudre le chemin
    expanded_path_str = os.path.expanduser(path_to_delete_str) if path_to_delete_str.startswith('~') else path_to_delete_str
    abs_path_str_to_delete = str((app.get_cwd() / expanded_path_str).resolve() if not os.path.isabs(expanded_path_str) else Path(expanded_path_str).resolve())
    
    mcp_params = [abs_path_str_to_delete, recursive_flag]
    response = await app.send_mcp_request(None, "mcp.fs.delete", mcp_params)
    # Utiliser la fonction de formatage générique pour la réponse
    await _rich_format_mcp_fs_list(console, {"result": [response.get("result", {})]} if response else {}, "mcp.fs.delete") # Astuce pour réutiliser un peu

async def cmd_licence(args_list: List[str], app: 'ShellApp'):
    """Displays current licence information from the gateway."""
    response = await app.send_mcp_request(None, "mcp.licence.check", [])
    await _fs_command_proxy("mcp.licence.check", [], app) # Réutiliser pour formatage (va échouer car pas d'args attendu)
    # Mieux : formatter directement
    if not response: app.console.print("[prompt.error]No response or connection failed.[/prompt.error]"); return
    if "error" in response: app.console.print(f"[prompt.error]MCP Error: {response['error']['message']}[/prompt.error]")
    elif "result" in response: app.console.print(Syntax(json.dumps(response["result"], indent=2), "json", theme="native", line_numbers=True, background_color="default"))
    else: app.console.print(Syntax(json.dumps(response, indent=2), "json", theme="native", background_color="default"))


async def cmd_llm(args_list: List[str], app: 'ShellApp'):
    """
    Sends a chat prompt to the LLM. Usage: llm "Prompt text" '[{"model":"alias"}]'
    Example: llm "What is the capital of France?"
    Example: llm "Translate to Spanish: Hello World" '{"model": "gpt-4o"}'
    """
    console = app.console
    if not args_list:
        console.print(Text("Usage: llm \"<prompt_text>\" ['<json_options_dict_string>']", style="yellow"))
        console.print(Text("Example: llm \"Tell me a joke about developers.\"", style="yellow")); return

    prompt_str = args_list[0]
    options_str = args_list[1] if len(args_list) > 1 else "{}" # Doit être une chaîne JSON valide
    
    llm_options_dict: Dict[str, Any] = {}
    try:
        llm_options_dict = json.loads(options_str) # Parse la chaîne JSON en dictionnaire
        if not isinstance(llm_options_dict, dict):
            raise ValueError("LLM options must be a valid JSON dictionary string.")
    except (json.JSONDecodeError, ValueError) as e:
        console.print(f"[prompt.error]Invalid LLM options JSON string: {e}[/prompt.error]"); return

    messages_for_llm_chat = [{"role": "user", "content": prompt_str}]
    
    if not await app.ensure_connection():
        console.print("[prompt.error]LLM: Gateway connection failed. Cannot send prompt.[/prompt.error]"); return
    
    # Assurer que websocket est valide après ensure_connection
    if app.mcp_websocket:
        await stream_llm_chat_to_console(
            app.console, app.mcp_websocket, app.pending_responses, 
            messages_for_llm_chat, llm_options_dict
        )
    else:
        console.print("[prompt.error]LLM: Gateway WebSocket unavailable after connection attempt.[/prompt.error]")