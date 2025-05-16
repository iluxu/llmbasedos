# llmbasedos/shell_utils.py
import asyncio
import json
import uuid
import logging
from typing import List, Dict, Any, Optional, AsyncGenerator # Added AsyncGenerator for type hint clarity

import websockets # type: ignore # For WebSocketClientProtocol type hint
from rich.console import Console # For Console type hint
from rich.text import Text
from rich.syntax import Syntax

logger = logging.getLogger("llmbasedos.shell.utils") # Shell components share this logger namespace

async def stream_llm_chat_to_console(
    console: Console, # Passed in
    mcp_websocket_ref: Optional[websockets.WebSocketClientProtocol], # Passed from ShellApp
    pending_responses_ref: Dict[str, asyncio.Future],                # Passed from ShellApp
    messages: List[Dict[str, str]],
    llm_options: Optional[Dict[str, Any]] = None
) -> Optional[str]: # Returns full response text or None on error
    """Handles streaming mcp.llm.chat and printing to console."""
    if not mcp_websocket_ref or not mcp_websocket_ref.open:
        console.print("[bold red]LLM Stream: Not connected to gateway.[/]")
        # Attempt to reconnect should be handled by the caller (ShellApp)
        return None

    actual_llm_options = {"stream": True} # Force stream for console
    if llm_options:
        actual_llm_options.update(llm_options)
        actual_llm_options["stream"] = True # Ensure stream is not overridden to false

    request_id = str(uuid.uuid4()) # Unique ID for this stream request
    request_payload = {
        "jsonrpc": "2.0",
        "method": "mcp.llm.chat",
        "params": [messages, actual_llm_options],
        "id": request_id
    }

    logger.debug(f"LLM Stream SEND (ID {request_id}) via shell_utils: {str(request_payload)[:200]}...")
    try:
        await mcp_websocket_ref.send(json.dumps(request_payload))
    except websockets.exceptions.ConnectionClosed as e_send:
        logger.error(f"LLM Stream: Connection closed while sending request: {e_send}")
        console.print("\n[bold red]LLM Stream: Connection to gateway closed before sending request.[/]")
        # ShellApp should detect this and mark connection as dead.
        return None # Indicate failure

    console.print(Text("Assistant: ", style="bold blue"), end="")
    full_response_text = ""
    stream_active = True
    try:
        while stream_active:
            # Each chunk of the stream will use the same request_id.
            # The ShellApp's _response_listener_task puts responses into a queue for this ID,
            # or resolves a per-chunk future.
            # Let's assume a per-chunk future model for now as it's simpler for one stream at a time.
            chunk_future: asyncio.Future = asyncio.Future()
            pending_responses_ref[request_id] = chunk_future # For the *next* chunk
            
            stream_response_chunk: Dict[str, Any]
            try:
                # Timeout for receiving each chunk from the LLM/gateway
                stream_response_chunk = await asyncio.wait_for(chunk_future, timeout=120.0) 
            except asyncio.TimeoutError:
                console.print("\n[bold red]LLM Stream: Timeout waiting for next chunk.[/]")
                # Do not remove from pending_responses_ref here, listener might still resolve it late.
                # Or, if listener pops, this is fine. Assuming listener pops.
                # If listener doesn't pop on timeout, then `pending_responses_ref.pop(request_id, None)` here.
                # Current luca.py listener pops.
                stream_active = False; continue # Break outer while
            # No finally block needed here for pop, listener handles it.
            
            if "error" in stream_response_chunk:
                err_data = stream_response_chunk['error']
                console.print(f"\n[bold red]LLM Error (Code {err_data.get('code')}): {err_data.get('message', 'Unknown stream error')}[/]")
                if err_data.get('data'):
                     console.print(Syntax(json.dumps(err_data['data'], indent=2), "json", theme="native"))
                return None # Error ends the stream

            result_chunk = stream_response_chunk.get("result", {})
            chunk_type = result_chunk.get("type")

            if chunk_type == "llm_chunk":
                content_data = result_chunk.get("content", {})
                delta_content = ""
                if isinstance(content_data, dict): # OpenAI-like structure
                    delta_content = content_data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                # Add other potential structures if gateway normalizes differently
                
                if delta_content:
                    console.print(delta_content, end="")
                    # console.flush() # Rich console auto-flushes with end="" usually
                    full_response_text += delta_content
            elif chunk_type == "llm_stream_end":
                console.print() # Ensure newline after stream fully ends
                stream_active = False # End the while loop
            else:
                logger.warning(f"LLM Stream: Unexpected chunk type '{chunk_type}' for ID {request_id}: {str(result_chunk)[:100]}")
                # console.print(f"\n[yellow](Debug: Rcvd stream data: {str(result_chunk)[:100]})[/]")
                # If it's not an error and not an end, what to do?
                # Could be metadata. For now, assume only chunk/end/error are valid during active stream.
                # If protocol allows other messages with same ID during stream, this needs adjustment.
                # For now, consider unknown type as potentially problematic for this simple handler.
                # console.print(f"\n[yellow]LLM Stream: Ended due to unexpected data type: {chunk_type}.[/]")
                # stream_active = False # Option: end stream on unknown type

        return full_response_text

    except websockets.exceptions.ConnectionClosed:
        console.print("\n[bold red]LLM Stream: Connection to gateway closed.[/]")
        return full_response_text # Return what was received so far
    except Exception as e_stream_outer:
        # This catches errors in the while loop logic itself, or unhandled from await_for
        console.print(f"\n[bold red]LLM Stream: General error during processing: {e_stream_outer}[/]")
        logger.error(f"LLM stream processing error (shell_utils): {e_stream_outer}", exc_info=True)
        return full_response_text
