# llmbasedos/shell/builtin_cmds.py
import os
import json
from pathlib import Path
import shlex # For parsing arguments string
from typing import List, Any, Dict, Callable, Awaitable, Optional # Type hints

from rich.console import Console # For type hint
from rich.table import Table
from rich.text import Text
from rich.syntax import Syntax

# Import ShellApp type for type hinting (requires careful structuring or TYPE_CHECKING)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .luca import ShellApp # For type hinting ShellApp instance

# Shell utils for factored out logic (e.g. LLM streaming)
from .shell_utils import stream_llm_chat_to_console

BUILTIN_COMMAND_LIST = ["exit", "quit", "help", "connect", "cd", "pwd", "ls", "dir", "cat", "rm", "licence", "llm"]

# Note: All builtin command handlers now receive (args_list: List[str], app: ShellApp)
# The first arg `args_list` is the result of shlex.split() on the raw argument string.
# The second arg `app` is the instance of ShellApp providing access to CWD, send_mcp_request, etc.

async def cmd_exit(args_list: List[str], app: 'ShellApp'):
    """Exits the luca-shell."""
    app.console.print("Exiting luca-shell...") # Use app's console
    raise EOFError # Signals prompt_toolkit to exit

async def cmd_quit(args_list: List[str], app: 'ShellApp'):
    """Alias for exit."""
    await cmd_exit(args_list, app)

async def cmd_help(args_list: List[str], app: 'ShellApp'):
    """Shows available commands or help for a specific command."""
    console_ref = app.console # Use app's console for output
    if not args_list:
        console_ref.print("[bold]Available luca-shell built-in commands:[/bold]")
        tbl = Table(title="Built-in Commands", show_header=True, header_style="bold magenta")
        tbl.add_column("Command", style="dim", width=15); tbl.add_column("Description")
        for cmd_n in BUILTIN_COMMAND_LIST:
            handler = getattr(sys.modules[__name__], f"cmd_{cmd_n}", None)
            doc = handler.__doc__.strip().splitlines()[0] if handler and handler.__doc__ else "No description."
            tbl.add_row(cmd_n, doc)
        console_ref.print(tbl)
        console_ref.print(f"\n[bold]Available MCP commands ({len(app.available_mcp_commands)}):[/bold] {', '.join(app.available_mcp_commands[:10])}{'...' if len(app.available_mcp_commands)>10 else ''}")
        console_ref.print("Type 'help <builtin_command>' for details on built-ins.")
        console_ref.print("For MCP methods, use 'mcp.listCapabilities' or refer to protocol docs.")
    else:
        cmd_to_help = args_list[0]
        handler = getattr(sys.modules[__name__], f"cmd_{cmd_to_help}", None)
        if handler and handler.__doc__: console_ref.print(f"[bold]Help for '{cmd_to_help}':[/bold]\n{handler.__doc__.strip()}")
        else: console_ref.print(f"No help for builtin '{cmd_to_help}'. Is it an MCP command?")

async def cmd_connect(args_list: List[str], app: 'ShellApp'):
    """Attempts to (re)connect to the MCP gateway."""
    console_ref = app.console
    console_ref.print("Attempting to reconnect to gateway...")
    if await app.ensure_connection(force_reconnect=True): # Use ShellApp's connection method
        console_ref.print("[green]Successfully connected/reconnected to MCP Gateway.[/]")
    else:
        console_ref.print("[prompt.error]Failed to connect. Check gateway status and URL.[/]")


async def cmd_cd(args_list: List[str], app: 'ShellApp'):
    """Changes the current working directory. Usage: cd <path>"""
    console_ref = app.console
    target_path_str = args_list[0] if args_list else os.path.expanduser("~")
    
    current_cwd = app.get_cwd() # Get CWD from ShellApp state
    new_path = Path(target_path_str)
    if not new_path.is_absolute(): new_path = (current_cwd / new_path).resolve()
    else: new_path = new_path.resolve()

    # Verify path is a directory via mcp.fs.list (also checks existence/perms)
    # Pass None for request_id_override for simple req/resp
    response = await app.send_mcp_request(None, "mcp.fs.list", [str(new_path)])
    if response and "result" in response:
        app.set_cwd(new_path) # Update CWD in ShellApp state
    elif response and "error" in response:
        console_ref.print(f"[prompt.error]cd: {response['error']['message']} ('{new_path}')[/]")
    else: console_ref.print(f"[prompt.error]cd: Error verifying path '{new_path}'. No/invalid gateway response.[/]")

async def cmd_pwd(args_list: List[str], app: 'ShellApp'):
    """Prints the current working directory."""
    app.console.print(str(app.get_cwd()))


async def _rich_format_mcp_response(console_ref: Console, mcp_method: str, response: Optional[Dict[str,Any]]):
    """Helper to format MCP responses using Rich."""
    if not response: console_ref.print("[prompt.error]No response/Connection failed.[/]"); return

    if "error" in response:
        err = response["error"]
        console_ref.print(f"[prompt.error]MCP Error (Code {err.get('code')}): {err.get('message')}[/]")
        if "data" in err: console_ref.print(Syntax(json.dumps(err['data'],indent=2),"json",theme="native",background_color="default"))
    elif "result" in response:
        result = response["result"]
        if mcp_method == "mcp.fs.list" and isinstance(result, list):
            # (Rich table formatting for ls - same as before)
            table = Table(show_header=True, header_style="bold cyan", title=f"Contents of {response.get('_request_path', 'directory')}")
            table.add_column("Type", width=10); table.add_column("Size", justify="right", width=10)
            table.add_column("Modified", width=19); table.add_column("Name")
            for item in sorted(result, key=lambda x: (x.get('type')!='directory', x.get('name','').lower())):
                itype=item.get("type",""); color="blue" if itype=="directory" else "green" if itype=="file" else "magenta" if itype=="symlink" else "bright_black"
                sz=str(item.get("size",-1)); mod_iso=item.get("modified_at","")
                if itype=="directory": sz="[DIR]"
                elif item.get("size",-1)>=0: n=item["size"]; units=["B","KB","MB","GB","TB"]; i=0
                while n>=1024 and i<len(units)-1: n/=1024.0; i+=1; sz=f"{n:.1f}{units[i]}"
                else: sz=f"{n}{units[i]}" # Bytes
                mod_disp = datetime.fromisoformat(mod_iso.replace("Z","+00:00")).strftime("%Y-%m-%d %H:%M:%S") if mod_iso else "N/A"
                table.add_row(Text(itype,style=color),sz,mod_disp,Text(item.get("name","?"),style=color))
            console_ref.print(table)
        elif mcp_method == "mcp.fs.read" and isinstance(result, dict):
            # (Rich syntax highlighting for cat - same as before)
            content=result.get("content",""); enc=result.get("encoding"); mime=result.get("mime_type","application/octet-stream")
            if enc=="text":
                lexer=mime.split('/')[-1].split('+')[0].lower() if any(s in mime for s in ["json","xml","python","markdown","html","css","javascript","yaml","x-sh","toml"]) else "text"
                if lexer in ["vnd.yaml", "x-yaml"]: lexer="yaml"
                if lexer in ["x-python"]: lexer="python"
                if lexer in ["x-shellscript", "x-sh"]: lexer="bash"
                console_ref.print(Syntax(content,lexer,theme="native",line_numbers=True,word_wrap=True, background_color="default"))
            elif enc=="base64": console_ref.print(f"[yellow]Base64 (MIME: {mime}):[/]\n{content[:500]}{'...' if len(content)>500 else ''}")
            else: console_ref.print(str(result)) # Fallback
        elif isinstance(result, (dict, list)): # Default JSON for other results
            console_ref.print(Syntax(json.dumps(result,indent=2),"json",theme="native",line_numbers=True,background_color="default"))
        else: console_ref.print(str(result)) # Simple string, number, bool
    else: console_ref.print(Syntax(json.dumps(response,indent=2),"json",theme="native",background_color="default"))


async def _fs_command_proxy(mcp_method: str, args_list: List[str], app: 'ShellApp', num_path_args: int = 1, other_params_start_idx: Optional[int] = None):
    """Proxies common FS commands to MCP, resolving paths and formatting output."""
    console_ref = app.console
    if not args_list and num_path_args > 0: # Requires at least one path arg usually
        console_ref.print(f"[prompt.error]Usage: {mcp_method.split('.')[-1]} <path>{' [other_args...]' if other_params_start_idx else ''}[/]")
        return

    current_cwd = app.get_cwd()
    mcp_params: List[Any] = []
    request_path_for_ls_title = "" # For "ls" title

    for i in range(min(num_path_args, len(args_list))):
        path_str = args_list[i]
        # Robust path resolution, also handles common cases like `~`
        expanded_path_str = os.path.expanduser(path_str) if path_str.startswith('~') else path_str
        
        abs_path: Path
        if os.path.isabs(expanded_path_str): abs_path = Path(expanded_path_str).resolve()
        else: abs_path = (current_cwd / expanded_path_str).resolve()
        mcp_params.append(str(abs_path))
        if i == 0 and mcp_method == "mcp.fs.list": request_path_for_ls_title = str(abs_path)

    # Add remaining non-path arguments
    start_idx = other_params_start_idx if other_params_start_idx is not None else num_path_args
    if len(args_list) > start_idx:
        for arg_s in args_list[start_idx:]:
            try: mcp_params.append(json.loads(arg_s)) # Try JSON for bools, numbers, etc.
            except json.JSONDecodeError: mcp_params.append(arg_s) # Fallback to string

    response = await app.send_mcp_request(None, mcp_method, mcp_params)
    if response and mcp_method == "mcp.fs.list": response['_request_path'] = request_path_for_ls_title # Hack to pass path to formatter
    await _rich_format_mcp_response(console_ref, mcp_method, response)

async def cmd_ls(args_list: List[str], app: 'ShellApp'):
    """Lists files and directories. Usage: ls [path]"""
    path_arg_str = args_list[0] if args_list else "." # Default to current dir
    await _fs_command_proxy("mcp.fs.list", [path_arg_str], app, num_path_args=1)

async def cmd_dir(args_list: List[str], app: 'ShellApp'):
    """Alias for ls."""
    await cmd_ls(args_list, app)

async def cmd_cat(args_list: List[str], app: 'ShellApp'):
    """Displays file content. Usage: cat <path> [text|base64]"""
    if not args_list: app.console.print("[prompt.error]Usage: cat <path> [text|base64][/]"); return
    await _fs_command_proxy("mcp.fs.read", args_list, app, num_path_args=1, other_params_start_idx=1)

async def cmd_rm(args_list: List[str], app: 'ShellApp'):
    """Deletes a file or directory. Usage: rm <path> [-r|--recursive] [--force|-f]"""
    console_ref = app.console
    if not args_list: console_ref.print("[prompt.error]Usage: rm <path> [-r] [--force][/]"); return
    
    path_to_delete = args_list[0]; recursive_flag = False; force_flag = False
    if "-r" in args_list or "--recursive" in args_list: recursive_flag = True
    if "--force" in args_list or "-f" in args_list: force_flag = True

    if not force_flag:
        console_ref.print(f"[yellow]Delete '{path_to_delete}'{ ' recursively' if recursive_flag else ''}? Add --force or -f to confirm. Skipping.[/]")
        return
    
    # Call mcp.fs.delete directly, _fs_command_proxy is too generic for rm's specific bool param
    current_cwd = app.get_cwd()
    expanded_path_str = os.path.expanduser(path_to_delete) if path_to_delete.startswith('~') else path_to_delete
    abs_path_str = str((current_cwd / expanded_path_str).resolve() if not os.path.isabs(expanded_path_str) else Path(expanded_path_str).resolve())
    
    mcp_params_rm = [abs_path_str, recursive_flag]
    response = await app.send_mcp_request(None, "mcp.fs.delete", mcp_params_rm)
    await _rich_format_mcp_response(console_ref, "mcp.fs.delete", response)


async def cmd_licence(args_list: List[str], app: 'ShellApp'):
    """Displays current licence information from the gateway."""
    response = await app.send_mcp_request(None, "mcp.licence.check", [])
    await _rich_format_mcp_response(app.console, "mcp.licence.check", response)

async def cmd_llm(args_list: List[str], app: 'ShellApp'):
    """
    Sends a chat prompt to the LLM. Usage: llm "Prompt" [{"model":"name"}]
    Example: llm "What is the capital of France?"
    Example: llm "Translate: Hello" {"model": "gpt-4-turbo"}
    """
    console_ref = app.console
    if not args_list:
        console_ref.print(Text("Usage: llm \"<prompt_text>\" [json_options_dict_string]", style="yellow"))
        console_ref.print(Text("Example: llm \"What is the capital of France?\"", style="yellow")); return

    prompt_str = args_list[0]
    options_dict_str = args_list[1] if len(args_list) > 1 else "{}"
    
    llm_opts: Optional[Dict[str, Any]] = {}
    try:
        llm_opts = json.loads(options_dict_str)
        if not isinstance(llm_opts, dict): raise ValueError("LLM options must be a JSON dictionary.")
    except (json.JSONDecodeError, ValueError) as e:
        console_ref.print(f"[prompt.error]Invalid LLM options JSON: {e}[/]"); return

    messages_for_llm = [{"role": "user", "content": prompt_str}]
    # Use ShellApp's websocket and pending_responses for the stream_llm_chat_to_console util
    if not app.mcp_websocket or not app.mcp_websocket.open:
        if not await app.ensure_connection(): # Try to connect if not already
            console_ref.print("[prompt.error]LLM: Gateway connection failed. Cannot send prompt.[/]")
            return
    
    # Ensure websocket is not None after ensure_connection attempt.
    if app.mcp_websocket: # Should be true if ensure_connection succeeded
        await stream_llm_chat_to_console(
            console_ref, app.mcp_websocket, app.pending_responses, messages_for_llm, llm_opts
        )
    else: # Should have been caught by ensure_connection print, but defensive
        console_ref.print("[prompt.error]LLM: Gateway WebSocket unavailable after connection attempt.[/]")

# Placeholder for test_cd.py
# test_cd.py would need a running gateway and fs server, or mocks.
# For now, just an empty file.
