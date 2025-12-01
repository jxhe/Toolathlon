#!/usr/bin/env python3
"""
Toolathlon Remote Evaluation Client

Submit evaluation tasks to remote Toolathlon server.
Supports both public API mode and private (local vLLM) mode.
"""

import asyncio
import json
import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


import httpx
import typer

app = typer.Typer(help="Toolathlon Remote Evaluation Client")

# ===== Configuration =====
DEFAULT_SERVER_HOST = "localhost"
DEFAULT_SERVER_PORT = 8080
DEFAULT_WS_PROXY_PORT = 8081
TIMEOUT_SECONDS = 240 * 60  # 240 minutes
POLL_INTERVAL_PUBLIC = 10  # seconds
POLL_INTERVAL_PRIVATE = 5  # seconds

# ===== Helper Functions =====

def ensure_parent_dir(file_path: str):
    """Ensure parent directory exists for a file path"""
    parent = Path(file_path).parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)

async def cancel_job_on_server(server_url: str, job_id: str, reason: str = "Client error"):
    """Cancel job on server (best effort, don't fail if it doesn't work)"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{server_url}/cancel_job",
                params={"job_id": job_id}
            )
            log(f"Notified server to cancel job: {reason}")
    except Exception as e:
        log(f"Warning: Failed to notify server about cancellation: {e}")

# ===== Logging Setup =====

class UTCFormatter(logging.Formatter):
    """Custom formatter that adds both local and UTC timestamps"""
    def format(self, record):
        # Get local time
        local_time = datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')
        # Get UTC time
        utc_time = datetime.utcfromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')
        # Add both to the record
        record.local_time = local_time
        record.utc_time = utc_time
        return super().format(record)

def setup_logging(log_file: str):
    """Setup logging to file only (background worker should not output to terminal)"""
    ensure_parent_dir(log_file)

    # Create file handler with write mode to clear previous logs
    file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
    file_handler.setLevel(logging.INFO)

    formatter = UTCFormatter('[%(local_time)s][UTC %(utc_time)s] %(message)s')
    file_handler.setFormatter(formatter)

    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)

def log(message: str):
    """Log a message and flush immediately"""
    logging.info(message)
    # Force flush all handlers to ensure real-time writing
    for handler in logging.getLogger().handlers:
        handler.flush()

def anonymize_job_id(job_id: str) -> str:
    """Anonymize job_id by showing only first 2 and last 2 characters"""
    if not job_id or len(job_id) <= 6:
        return job_id
    return f"{job_id[:6]}{'*' * (len(job_id) - 8)}{job_id[-2:]}"

# ===== Worker Functions =====

async def public_worker(
    server_url: str,
    job_id: str,
    output_file: str,
    log_file: str,
    server_log_file: str,
    traj_log_file: Optional[str] = None
):
    """
    Background worker for public mode
    Only polls status, no LLM request handling needed
    """
    # Ensure all output directories exist
    ensure_parent_dir(output_file)
    ensure_parent_dir(server_log_file)

    setup_logging(log_file)

    log("="*60)
    log("Toolathlon Eval Client - Public Mode Worker")
    log(f"Job ID: {job_id}")
    log(f"Server: {server_url}")
    log("="*60)

    # Setup signal handlers for graceful shutdown
    import signal

    def signal_handler(signum, frame):
        log(f"\n!!! Received signal {signum}, shutting down...")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        start_time = time.time()
        server_log_offset = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                elapsed = time.time() - start_time

                # Check timeout
                if elapsed > TIMEOUT_SECONDS:
                    log(f"ERROR: Task exceeded {TIMEOUT_SECONDS//60} minutes timeout")
                    await cancel_job_on_server(server_url, job_id, "Timeout")
                    sys.exit(1)

                # Pull server log
                try:
                    resp = await client.get(
                        f"{server_url}/get_server_log",
                        params={"job_id": job_id, "offset": server_log_offset}
                    )
                    log_data = resp.json()

                    if log_data.get("content"):
                        with open(server_log_file, 'a') as f:
                            f.write(log_data["content"])
                        server_log_offset = log_data["offset"]

                except Exception as e:
                    log(f"Warning: Failed to fetch server log: {e}")

                # Poll job status
                try:
                    resp = await client.get(
                        f"{server_url}/poll_job_status",
                        params={"job_id": job_id}
                    )
                    status_data = resp.json()
                    status = status_data.get("status")

                    if status == "completed":
                        log("Task completed successfully!")

                        # Final server log pull
                        try:
                            resp = await client.get(
                                f"{server_url}/get_server_log",
                                params={"job_id": job_id, "offset": server_log_offset}
                            )
                            log_data = resp.json()
                            if log_data.get("content"):
                                with open(server_log_file, 'a') as f:
                                    f.write(log_data["content"])
                        except:
                            pass

                        # Save results
                        eval_stats = status_data.get("eval_stats", {})
                        with open(output_file, 'w') as f:
                            json.dump(eval_stats, f, indent=2)

                        # Save traj_log_all if provided
                        if traj_log_file and status_data.get("traj_log_all"):
                            ensure_parent_dir(traj_log_file)
                            with open(traj_log_file, 'w') as f:
                                f.write(status_data["traj_log_all"])
                            log(f"Trajectory log saved to: {traj_log_file}")

                        log(f"Results saved to: {output_file}")
                        log(f"Server log saved to: {server_log_file}")
                        log("="*60)
                        sys.exit(0)

                    elif status in ["failed", "timeout", "cancelled"]:
                        error = status_data.get("error", "Unknown error")
                        log(f"Task failed/cancelled: {error}")

                        # Final server log pull
                        try:
                            resp = await client.get(
                                f"{server_url}/get_server_log",
                                params={"job_id": job_id, "offset": server_log_offset}
                            )
                            log_data = resp.json()
                            if log_data.get("content"):
                                with open(server_log_file, 'a') as f:
                                    f.write(log_data["content"])
                        except:
                            pass

                        log("="*60)
                        sys.exit(1)

                    else:
                        elapsed_min = int(elapsed / 60)
                        log(f"Task running... (elapsed: {elapsed_min} minutes)")

                except Exception as e:
                    log(f"Error polling status: {e}")

                await asyncio.sleep(POLL_INTERVAL_PUBLIC)

    except KeyboardInterrupt:
        log("\n!!! Client interrupted by user (Ctrl+C)")
        await cancel_job_on_server(server_url, job_id, "Client interrupted")
        sys.exit(1)
    except Exception as e:
        log(f"\n!!! FATAL ERROR in client: {e}")
        import traceback
        log(traceback.format_exc())
        await cancel_job_on_server(server_url, job_id, f"Client error: {e}")
        sys.exit(1)

async def private_worker(
    server_url: str,
    job_id: str,
    client_id: str,
    vllm_url: str,
    vllm_api_key: Optional[str],
    output_file: str,
    log_file: str,
    server_log_file: str,
    ws_proxy_port: int,
    traj_log_file: Optional[str] = None
):
    """
    Background worker for private mode
    Starts simple_client_ws.py and monitors job status
    """
    # Ensure all output directories exist
    ensure_parent_dir(output_file)
    ensure_parent_dir(server_log_file)

    # delete the old log files if exists
    if os.path.exists(log_file):
        os.remove(log_file)
    if os.path.exists(server_log_file):
        os.remove(server_log_file)

    setup_logging(log_file)

    log("="*60)
    log("Toolathlon Eval Client - Private Mode Worker")
    log(f"Job ID: {job_id}")
    log(f"Client ID: {client_id}")
    log(f"vLLM URL: {vllm_url}")
    log(f"Server: {server_url}")
    log("="*60)

    ws_client_process = None
    ws_client_log_file = None

    # Setup signal handlers for graceful shutdown
    import signal

    def signal_handler(signum, frame):
        log(f"\n!!! Received signal {signum}, shutting down...")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        start_time = time.time()
        server_log_offset = 0
        should_exit = False
        exit_code = 0

        # Extract server host from server_url for WebSocket connection
        parsed = urlparse(server_url)
        ws_server_url = f"http://{parsed.hostname}:{ws_proxy_port}"

        # Start simple_client_ws.py
        log(f"[WS] Starting WebSocket client...")
        log(f"[WS] Connecting to: {ws_server_url}")
        ws_client_log = str(Path(log_file).parent / "ws_client.log")

        with open(ws_client_log, 'w') as log_f:
            ws_client_process = await asyncio.create_subprocess_exec(
                sys.executable, "simple_client_ws.py",
                "--server-url", ws_server_url,
                "--llm-base-url", vllm_url,
                "--llm-api-key", vllm_api_key or "",
                stdout=log_f,
                stderr=asyncio.subprocess.STDOUT
            )

        log(f"[WS] WebSocket client started (PID: {ws_client_process.pid})")
        log(f"[WS] Client log: {ws_client_log}")

        async def status_poller():
            """Poll job status and sync server log"""
            nonlocal should_exit, exit_code, server_log_offset

            async with httpx.AsyncClient(timeout=30.0) as client:
                while not should_exit:
                    try:
                        elapsed = time.time() - start_time

                        # Check timeout
                        if elapsed > TIMEOUT_SECONDS:
                            log(f"ERROR: Task exceeded {TIMEOUT_SECONDS//60} minutes timeout")
                            await cancel_job_on_server(server_url, job_id, "Timeout")
                            should_exit = True
                            exit_code = 1
                            return

                        # Pull server log
                        try:
                            resp = await client.get(
                                f"{server_url}/get_server_log",
                                params={"job_id": job_id, "offset": server_log_offset}
                            )
                            log_data = resp.json()

                            if log_data.get("content"):
                                with open(server_log_file, 'a') as f:
                                    f.write(log_data["content"])
                                server_log_offset = log_data["offset"]

                        except Exception as e:
                            log(f"Warning: Failed to fetch server log: {e}")

                        # Poll status
                        resp = await client.get(
                            f"{server_url}/poll_job_status",
                            params={"job_id": job_id}
                        )
                        status_data = resp.json()
                        status = status_data.get("status")

                        if status == "completed":
                            log("Task completed successfully!")

                            # Final server log pull
                            try:
                                resp = await client.get(
                                    f"{server_url}/get_server_log",
                                    params={"job_id": job_id, "offset": server_log_offset}
                                )
                                log_data = resp.json()
                                if log_data.get("content"):
                                    with open(server_log_file, 'a') as f:
                                        f.write(log_data["content"])
                            except:
                                pass

                            # Save results
                            eval_stats = status_data.get("eval_stats", {})
                            with open(output_file, 'w') as f:
                                json.dump(eval_stats, f, indent=2)

                            # Save traj_log_all if provided
                            if traj_log_file and status_data.get("traj_log_all"):
                                ensure_parent_dir(traj_log_file)
                                with open(traj_log_file, 'w') as f:
                                    f.write(status_data["traj_log_all"])
                                log(f"Trajectory log saved to: {traj_log_file}")

                            log(f"Results saved to: {output_file}")
                            log(f"Server log saved to: {server_log_file}")
                            log("="*60)

                            should_exit = True
                            exit_code = 0
                            return

                        elif status in ["failed", "timeout", "cancelled"]:
                            error = status_data.get("error", "Unknown error")
                            log(f"Task failed/cancelled: {error}")

                            # Final server log pull
                            try:
                                resp = await client.get(
                                    f"{server_url}/get_server_log",
                                    params={"job_id": job_id, "offset": server_log_offset}
                                )
                                log_data = resp.json()
                                if log_data.get("content"):
                                    with open(server_log_file, 'a') as f:
                                        f.write(log_data["content"])
                            except:
                                pass

                            log("="*60)
                            should_exit = True
                            exit_code = 1
                            return

                        else:
                            elapsed_min = int(elapsed / 60)
                            if elapsed_min % 5 == 0:  # Log every 5 minutes
                                log(f"Task running... (elapsed: {elapsed_min} minutes)")

                    except Exception as e:
                        log(f"Error in status poller: {e}")

                    await asyncio.sleep(POLL_INTERVAL_PRIVATE)

        # Run status poller
        await status_poller()

        sys.exit(exit_code)

    except KeyboardInterrupt:
        log("\n!!! Client interrupted by user (Ctrl+C)")
        await cancel_job_on_server(server_url, job_id, "Client interrupted")
        sys.exit(1)
    except Exception as e:
        log(f"\n!!! FATAL ERROR in client: {e}")
        import traceback
        log(traceback.format_exc())
        await cancel_job_on_server(server_url, job_id, f"Client error: {e}")
        sys.exit(1)
    finally:
        # Kill WebSocket client if running
        if ws_client_process:
            try:
                log(f"[WS] Stopping WebSocket client (PID: {ws_client_process.pid})")
                ws_client_process.kill()
                await ws_client_process.wait()
            except:
                pass

# ===== CLI Commands =====

@app.command()
def run(
    mode: str = typer.Option(..., help="Mode: 'public' or 'private'"),
    base_url: str = typer.Option(..., help="API base URL (public: OpenAI-compatible API, private: local vLLM)"),
    model_name: str = typer.Option(..., help="Model name"),
    output_file: str = typer.Option(..., help="Output file path for eval_stats.json"),
    log_file: str = typer.Option(..., help="Client log file path"),
    server_log: str = typer.Option(..., help="Server execution log file path"),
    traj_log: Optional[str] = typer.Option(None, help="Trajectory log file path for traj_log_all.jsonl (optional)"),
    api_key: Optional[str] = typer.Option(None, help="API key (optional)"),
    workers: int = typer.Option(10, help="Number of parallel workers"),
    server_host: str = typer.Option(DEFAULT_SERVER_HOST, help="Evaluation server host (without port)"),
    server_port: int = typer.Option(DEFAULT_SERVER_PORT, help="Evaluation server port"),
    ws_proxy_port: int = typer.Option(DEFAULT_WS_PROXY_PORT, help="WebSocket proxy port (for private mode)"),
    job_id: Optional[str] = typer.Option(None, help="Custom job ID (optional, will generate UUID if not provided)"),
    tasks: Optional[str] = typer.Option(None, help="Comma-separated task names to run (optional, e.g., 'task1,task2' or just 'task1'). If not specified, all tasks will be run."),
):
    """
    Submit and run an evaluation task.

    This command will:
    1. Submit the task to the server
    2. Start a background daemon process
    3. Return immediately (non-blocking)
    """

    if mode not in ["public", "private"]:
        typer.echo("Error: mode must be 'public' or 'private'", err=True)
        raise typer.Exit(1)

    # Build server URL
    server_url = f"http://{server_host}:{server_port}"

    typer.echo("Submitting evaluation task to server...")
    typer.echo(f"  Mode: {mode}")
    typer.echo(f"  Model: {model_name}")
    typer.echo(f"  Workers: {workers}")
    typer.echo(f"  Server: {server_url}")
    if tasks:
        typer.echo(f"  Tasks: {tasks}")
    if mode == "private":
        typer.echo(f"  WebSocket Proxy Port: {ws_proxy_port}")

    # Submit task
    try:
        import httpx

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{server_url}/submit_evaluation",
                json={
                    "mode": mode,
                    "base_url": base_url,
                    "api_key": api_key,
                    "model_name": model_name,
                    "workers": workers,
                    "custom_job_id": job_id,  # Pass custom job_id if provided
                    "tasks": tasks  # Pass tasks filter if provided
                }
            )

            if resp.status_code != 200:
                error_data = resp.json()
                typer.echo(f"\n❌ Task submission failed:", err=True)
                typer.echo(f"   {error_data.get('detail', 'Unknown error')}", err=True)
                raise typer.Exit(1)

            result = resp.json()
            final_job_id = result["job_id"]
            client_id = result.get("client_id")
            warning = result.get("warning")

    except httpx.ConnectError:
        typer.echo(f"\n❌ Cannot connect to server at {server_url}", err=True)
        typer.echo("   Please check if the server is running.", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"\n❌ Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo("\n✓ Task submitted successfully!")
    typer.echo(f"  Job ID: {final_job_id}")
    if client_id:
        typer.echo(f"  Client ID: {client_id}")

    # Display warning if job_id already exists
    if warning:
        typer.echo(f"\n⚠️  WARNING: {warning}", err=False)
        typer.echo(f"   Only use the same job ID if you want to resume an incomplete task.", err=False)

    # Start background worker using subprocess with nohup-like behavior
    import subprocess

    # Build Python code to run worker directly
    if mode == "public":
        traj_log_arg = f"'{traj_log}'" if traj_log else "None"
        worker_code = f"""
import asyncio
import sys
sys.path.insert(0, '{os.path.abspath(os.path.dirname(__file__))}')
from eval_client import public_worker
asyncio.run(public_worker('{server_url}', '{final_job_id}', '{output_file}', '{log_file}', '{server_log}', {traj_log_arg}))
"""
    else:  # private
        api_key_arg = f"'{api_key}'" if api_key else "None"
        traj_log_arg = f"'{traj_log}'" if traj_log else "None"
        worker_code = f"""
import asyncio
import sys
sys.path.insert(0, '{os.path.abspath(os.path.dirname(__file__))}')
from eval_client import private_worker
asyncio.run(private_worker('{server_url}', '{final_job_id}', '{client_id}', '{base_url}', {api_key_arg}, '{output_file}', '{log_file}', '{server_log}', {ws_proxy_port}, {traj_log_arg}))
"""

    # Start detached background process
    process = subprocess.Popen(
        [sys.executable, '-c', worker_code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=os.getcwd()
    )

    typer.echo(f"\n✓ Background worker started (PID: {process.pid})")
    typer.echo(f"  Client log: {log_file}")
    typer.echo(f"  Server log: {server_log} (syncing...)")
    typer.echo(f"  Results: {output_file} (when complete)")
    if traj_log:
        typer.echo(f"  Trajectory log: {traj_log} (when complete)")
    typer.echo(f"\n📊 Monitor progress:")
    typer.echo(f"  tail -f {log_file}")
    typer.echo(f"\n❓ Check status:")
    typer.echo(f"  python eval_client.py status --job-id {final_job_id} --server-host {server_host} --server-port {server_port}")
    typer.echo(f"\n🛑 Cancel task:")
    typer.echo(f"  python eval_client.py cancel {final_job_id} --server-host {server_host} --server-port {server_port}")
    typer.echo()

@app.command()
def status(
    job_id: str = typer.Option(..., help="Job ID"),
    server_host: str = typer.Option(DEFAULT_SERVER_HOST, help="Evaluation server host"),
    server_port: int = typer.Option(DEFAULT_SERVER_PORT, help="Evaluation server port"),
):
    """Check the status of a submitted task"""

    server_url = f"http://{server_host}:{server_port}"

    try:
        import httpx

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{server_url}/poll_job_status",
                params={"job_id": job_id}
            )

            if resp.status_code != 200:
                typer.echo(f"Error: {resp.json()}", err=True)
                raise typer.Exit(1)

            data = resp.json()
            status = data.get("status")

            typer.echo(f"\nJob ID: {job_id}")
            typer.echo(f"Status: {status}")

            if status == "completed":
                typer.echo("✓ Task completed successfully!")
            elif status in ["failed", "timeout"]:
                typer.echo(f"✗ Task failed: {data.get('error', 'Unknown')}")
            elif status == "running":
                typer.echo("⏳ Task is still running...")
            else:
                typer.echo("? Status unknown (task may not exist)")

            typer.echo()

    except httpx.ConnectError:
        typer.echo(f"Cannot connect to server at {server_url}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

@app.command()
def cancel(
    job_id: str = typer.Argument(..., help="Job ID to cancel"),
    server_host: str = typer.Option(DEFAULT_SERVER_HOST, help="Evaluation server host"),
    server_port: int = typer.Option(DEFAULT_SERVER_PORT, help="Evaluation server port"),
):
    """Cancel a running task"""

    server_url = f"http://{server_host}:{server_port}"

    try:
        import httpx

        typer.echo(f"Cancelling job {job_id}...")

        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                f"{server_url}/cancel_job",
                params={"job_id": job_id}
            )

            if resp.status_code != 200:
                error = resp.json()
                typer.echo(f"❌ Error: {error.get('detail', 'Unknown error')}", err=True)
                raise typer.Exit(1)

            typer.echo(f"✓ Job {job_id} cancelled successfully")
            typer.echo(f"  - Process killed")
            typer.echo(f"  - Docker containers stopped and removed")

    except httpx.ConnectError:
        typer.echo(f"❌ Cannot connect to server at {server_url}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"❌ Error: {e}", err=True)
        raise typer.Exit(1)

@app.command()
def check(
    server_host: str = typer.Option(DEFAULT_SERVER_HOST, help="Evaluation server host"),
    server_port: int = typer.Option(DEFAULT_SERVER_PORT, help="Evaluation server port"),
):
    """Check if the server is available and idle"""

    server_url = f"http://{server_host}:{server_port}"

    try:
        import httpx

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{server_url}/check_server_status")

            if resp.status_code != 200:
                typer.echo("Error checking server status", err=True)
                raise typer.Exit(1)

            data = resp.json()

            if data.get("busy"):
                job_id = data.get('job_id', '')
                anonymized_id = anonymize_job_id(job_id)
                typer.echo("⏳ Server is currently busy")
                typer.echo(f"   Job ID: {anonymized_id}")
                typer.echo(f"   Mode: {data.get('mode')}")
                typer.echo(f"   Started: {data.get('started_at')}")
                typer.echo("\nPlease try again later.")
            else:
                typer.echo("✓ Server is idle and ready to accept tasks")

    except httpx.ConnectError:
        typer.echo(f"❌ Cannot connect to server at {server_url}", err=True)
        typer.echo("   Please check if the server is running.", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

if __name__ == "__main__":
    app()
