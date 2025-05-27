# llmbasedos_src/shell/builtin_cmds.py
import os
import json # Pour parser les arguments optionnels en JSON
import sys 
from pathlib import Path
from typing import List, Any, Dict, Optional

# Import ShellApp type pour l'annotation de type et les utilitaires Rich
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .luca import ShellApp # Utilisé pour l'annotation de type de 'app'

# Shell utils (si cmd_llm l'utilise directement)
from .shell_utils import stream_llm_chat_to_console # stream_llm_chat_to_console est nécessaire pour cmd_llm
from rich.text import Text # Pour formater certains messages

# Liste des commandes builtin (pour la complétion et l'aide)
BUILTIN_COMMAND_LIST = [
    "exit", "quit", "help", "connect", "cd", "pwd", 
    "ls", "dir", "cat", "rm", "licence", "llm"
]

# --- Implémentation des Commandes Built-in ---
# Nouvelle signature : async def cmd_nom_commande(args_list: List[str], app: 'ShellApp')

async def cmd_exit(args_list: List[str], app: 'ShellApp'):
    """Exits the luca-shell."""
    app.console.print("Exiting luca-shell...")
    raise EOFError # Signale à prompt_toolkit de quitter la boucle REPL

async def cmd_quit(args_list: List[str], app: 'ShellApp'):
    """Alias for the 'exit' command."""
    await cmd_exit(args_list, app)

async def cmd_help(args_list: List[str], app: 'ShellApp'):
    """Shows available commands or help for a specific command.
    Usage: help [builtin_command_name]
    """
    # Utiliser app.console qui est l'instance de Rich Console de ShellApp
    if not args_list:
        app.console.print("[bold]Available luca-shell built-in commands:[/bold]")
        # Assumer que ShellApp a une méthode pour créer une table Rich ou que Table est importé ici
        from rich.table import Table # Importer Table ici si besoin local
        tbl = Table(title="Built-in Commands", show_header=True, header_style="bold magenta")
        tbl.add_column("Command", style="dim", width=15)
        tbl.add_column("Description")
        
        for cmd_name_str in BUILTIN_COMMAND_LIST:
            handler_func = getattr(sys.modules[__name__], f"cmd_{cmd_name_str}", None)
            docstring = "No description available."
            if handler_func and handler_func.__doc__:
                docstring = handler_func.__doc__.strip().splitlines()[0]
            tbl.add_row(cmd_name_str, docstring)
        app.console.print(tbl)
        
        available_mcp_cmds = sorted(list(app.available_mcp_commands))
        if available_mcp_cmds:
            app.console.print(f"\n[bold]Available MCP commands ({len(available_mcp_cmds)} discovered):[/bold]")
            for i in range(0, len(available_mcp_cmds), 5):
                 app.console.print("  " + ", ".join(available_mcp_cmds[i:i+5]))
        else:
            app.console.print("\n[yellow]No MCP commands currently discovered. Try 'connect' or check gateway.[/yellow]")
            
        app.console.print("\nType 'help <builtin_command_name>' for details on built-ins.")
        app.console.print("For MCP methods, use 'mcp.listCapabilities' or check protocol documentation.")
    else:
        cmd_to_help_str = args_list[0]
        handler_func = getattr(sys.modules[__name__], f"cmd_{cmd_to_help_str}", None)
        if handler_func and handler_func.__doc__:
            app.console.print(f"[bold]Help for built-in command '{cmd_to_help_str}':[/bold]\n{handler_func.__doc__.strip()}")
        else:
            app.console.print(f"No help found for built-in command '{cmd_to_help_str}'. If it's an MCP command, its description can be found via 'mcp.listCapabilities'.")

async def cmd_connect(args_list: List[str], app: 'ShellApp'):
    """Attempts to (re)connect to the MCP gateway."""
    app.console.print("Attempting to (re)connect to MCP gateway...")
    if await app.ensure_connection(force_reconnect=True):
        app.console.print("[green]Successfully connected/reconnected to MCP Gateway.[/green]")
        app.console.print("Type 'mcp.hello' or 'mcp.listCapabilities' to see available remote commands.")
    else:
        app.console.print("[[error]Failed to connect[/]]. Check gateway status and URL configured in shell.")

async def cmd_cd(args_list: List[str], app: 'ShellApp'):
    """Changes the current working directory. Usage: cd <path>"""
    if not args_list:
        target_path_str = os.path.expanduser("~")
    else:
        target_path_str = args_list[0]
    
    current_cwd = app.get_cwd()
    expanded_path_str = os.path.expanduser(target_path_str)
    
    new_path_obj: Path
    if os.path.isabs(expanded_path_str):
        new_path_obj = Path(expanded_path_str).resolve()
    else:
        new_path_obj = (current_cwd / expanded_path_str).resolve()

    response = await app.send_mcp_request(None, "mcp.fs.list", [str(new_path_obj)]) 
    
    if response and "result" in response:
        app.set_cwd(new_path_obj)
    elif response and "error" in response:
        # Utiliser app._format_and_print_mcp_response pour afficher l'erreur MCP de manière standard
        await app._format_and_print_mcp_response("mcp.fs.list", response, request_path_for_ls=str(new_path_obj))
    else:
        app.console.print(f"[[error]cd error[/]]: Error verifying path '{new_path_obj}'. No or invalid response from gateway.")

async def cmd_pwd(args_list: List[str], app: 'ShellApp'):
    """Prints the current working directory managed by the shell."""
    app.console.print(str(app.get_cwd()))

async def cmd_ls(args_list: List[str], app: 'ShellApp'):
    """Lists files and directories. Usage: ls [path]"""
    path_arg_str = args_list[0] if args_list else "."
    
    expanded_path_str = os.path.expanduser(path_arg_str) if path_arg_str.startswith('~') else path_arg_str
    abs_path_obj = (app.get_cwd() / expanded_path_str).resolve() if not os.path.isabs(expanded_path_str) else Path(expanded_path_str).resolve()
    
    response = await app.send_mcp_request(None, "mcp.fs.list", [str(abs_path_obj)])
    await app._format_and_print_mcp_response("mcp.fs.list", response, request_path_for_ls=str(abs_path_obj))

async def cmd_dir(args_list: List[str], app: 'ShellApp'):
    """Alias for the 'ls' command."""
    await cmd_ls(args_list, app)

async def cmd_cat(args_list: List[str], app: 'ShellApp'):
    """Displays file content. Usage: cat <path> [text|base64]"""
    if not args_list:
        app.console.print("[[error]Usage[/]]: cat <path> [text|base64]")
        return

    path_str_arg = args_list[0]
    expanded_path_str = os.path.expanduser(path_str_arg) if path_str_arg.startswith('~') else path_str_arg
    abs_path_obj = (app.get_cwd() / expanded_path_str).resolve() if not os.path.isabs(expanded_path_str) else Path(expanded_path_str).resolve()
    
    mcp_params: List[Any] = [str(abs_path_obj)]
    if len(args_list) > 1: # Pour l'argument optionnel d'encodage
        mcp_params.append(args_list[1])

    response = await app.send_mcp_request(None, "mcp.fs.read", mcp_params)
    await app._format_and_print_mcp_response("mcp.fs.read", response)

async def cmd_rm(args_list: List[str], app: 'ShellApp'):
    """Deletes a file or directory. Usage: rm <path> [-r|--recursive] [--force|-f]"""
    if not args_list:
        app.console.print("[[error]Usage[/]]: rm <path> [-r|--recursive] [--force|-f]")
        return
    
    path_to_delete_str = args_list[0]
    recursive_flag = any(flag in args_list for flag in ["-r", "--recursive"])
    force_flag = any(flag in args_list for flag in ["-f", "--force"])

    if not force_flag:
        confirm_msg = f"Delete '{path_to_delete_str}'{' recursively' if recursive_flag else ''}? This is permanent. "
        app.console.print(f"[yellow]{confirm_msg}Add --force or -f to confirm. Skipping for now.[/yellow]")
        return

    expanded_path_str = os.path.expanduser(path_to_delete_str) if path_to_delete_str.startswith('~') else path_to_delete_str
    abs_path_str_to_delete = str((app.get_cwd() / expanded_path_str).resolve() if not os.path.isabs(expanded_path_str) else Path(expanded_path_str).resolve())
    
    mcp_params_for_rm = [abs_path_str_to_delete, recursive_flag]
    response = await app.send_mcp_request(None, "mcp.fs.delete", mcp_params_for_rm)
    await app._format_and_print_mcp_response("mcp.fs.delete", response, request_path_for_ls=abs_path_str_to_delete)

async def cmd_licence(args_list: List[str], app: 'ShellApp'):
    """Displays current licence information from the gateway."""
    response = await app.send_mcp_request(None, "mcp.licence.check", [])
    await app._format_and_print_mcp_response("mcp.licence.check", response)

async def cmd_llm(args_list: List[str], app: 'ShellApp'):
    """
    Sends a chat prompt to the LLM via mcp.llm.chat for a streaming response.
    Usage: llm "Your prompt text" ['<json_options_dict_string>']
    Example: llm "What is the capital of France?"
    Example: llm "Translate to Spanish: Hello World" '{"model": "gpt-4o"}'
    Note: Options dictionary string must be valid JSON.
    """
    if not args_list:
        app.console.print(Text("Usage: llm \"<prompt_text>\" ['<json_options_dict_string>']", style="yellow"))
        app.console.print(Text("Example: llm \"Tell me a joke about developers.\"", style="yellow")); return

    prompt_str = args_list[0]
    options_json_str = args_list[1] if len(args_list) > 1 else "{}" 
    
    llm_options_dict: Dict[str, Any] = {}
    try:
        llm_options_dict = json.loads(options_json_str)
        if not isinstance(llm_options_dict, dict):
            raise ValueError("LLM options, if provided, must be a valid JSON dictionary string.")
    except (json.JSONDecodeError, ValueError) as e:
        # Utiliser app.console et escape pour afficher l'erreur
        from rich.markup import escape # Importer escape localement si pas déjà global
        app.console.print(f"[[error]Invalid LLM options JSON string[/]]: {escape(str(e))}"); return

    messages_for_llm_chat = [{"role": "user", "content": prompt_str}]
    
    # stream_llm_chat_to_console s'occupera de ensure_connection via l'instance app
    await stream_llm_chat_to_console(
        app=app, # Passer l'instance de ShellApp
        # console=app.console, # <<< LIGNE SUPPRIMÉE, car app est passé
        messages=messages_for_llm_chat,
        llm_options=llm_options_dict
    )