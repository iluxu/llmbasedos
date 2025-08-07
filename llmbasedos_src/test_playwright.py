#!/usr/bin/env python3
"""
Debug script to test Playwright container startup independently
"""
import asyncio
import docker
import httpx
import time
import sys

async def test_playwright_container():
    """Test the Playwright container startup process"""
    docker_client = docker.from_env()
    container = None
    
    try:
        print("🔍 Checking Docker daemon connection...")
        print(f"Docker version: {docker_client.version()['Version']}")
        
        print("\n📥 Pulling Playwright MCP image (if needed)...")
        try:
            image = docker_client.images.get("mcr.microsoft.com/playwright/mcp:latest")
            print(f"✅ Image already exists: {image.short_id}")
        except docker.errors.ImageNotFound:
            print("⬇️  Image not found, pulling...")
            image = docker_client.images.pull("mcr.microsoft.com/playwright/mcp:latest")
            print(f"✅ Image pulled: {image.short_id}")
        
        print("\n🚀 Starting Playwright container...")
        container = docker_client.containers.run(
            "mcr.microsoft.com/playwright/mcp:latest",
            detach=True,
            auto_remove=True,
            network_mode="host",
            stdin_open=True,
            tty=True,
            environment={
                "DISPLAY": ":99"
            },
            mem_limit="1g",
            cap_add=["SYS_ADMIN"]
        )
        
        print(f"✅ Container started: {container.short_id}")
        print(f"📊 Container status: {container.status}")
        
        print("\n⏳ Waiting for container to be ready...")
        max_wait = 60
        start_time = time.time()
        check_interval = 2
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            while time.time() - start_time < max_wait:
                try:
                    # Reload container status
                    container.reload()
                    elapsed = time.time() - start_time
                    
                    print(f"⏱️  {elapsed:.1f}s - Container status: {container.status}")
                    
                    if container.status != "running":
                        logs = container.logs().decode('utf-8')
                        print(f"❌ Container not running. Logs:\n{logs}")
                        return False
                    
                    # Try to connect
                    print(f"🔗 Attempting connection to http://localhost:5678/sse...")
                    response = await client.get("http://localhost:5678/sse")
                    print(f"✅ Connection successful! Status: {response.status_code}")
                    print(f"📝 Response preview: {response.text[:200]}...")
                    
                    # Test a full session
                    print("\n🧪 Testing full session...")
                    return await test_full_session(client)
                    
                except httpx.ConnectError as e:
                    print(f"❌ Connection failed: {e}")
                    await asyncio.sleep(check_interval)
                    
                except Exception as e:
                    print(f"❌ Unexpected error: {e}")
                    return False
            
            print(f"⏰ Timeout reached after {max_wait}s")
            
            # Get final logs
            try:
                container.reload()
                logs = container.logs().decode('utf-8')
                print(f"📜 Final container logs:\n{logs}")
            except Exception as e:
                print(f"❌ Could not get logs: {e}")
                
            return False
            
    except Exception as e:
        print(f"💥 Error: {e}")
        return False
        
    finally:
        if container:
            print(f"\n🛑 Stopping container {container.short_id}...")
            try:
                container.stop(timeout=10)
                print("✅ Container stopped")
            except Exception as e:
                print(f"❌ Error stopping container: {e}")

async def test_full_session(client):
    """Test a complete scraping session"""
    try:
        # Get session URL
        print("🎫 Getting session URL...")
        sse_res = await client.get("http://localhost:5678/sse", 
                                  headers={"Accept": "text/event-stream"})
        session_url_part = sse_res.text.split("data: ")[1].strip()
        session_url = f"http://localhost:5678{session_url_part}"
        print(f"✅ Session URL: {session_url}")
        
        headers = {
            "Accept": "application/json, text/event-stream", 
            "Content-Type": "application/json"
        }
        
        # Initialize
        print("🏁 Initializing session...")
        init_payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"}
            },
            "id": "init-1"
        }
        init_response = await client.post(session_url, json=init_payload, headers=headers)
        print(f"✅ Initialize response: {init_response.status_code}")
        
        # Navigate
        test_url = "https://example.com"
        print(f"🌐 Navigating to {test_url}...")
        nav_payload = {
            "jsonrpc": "2.0",
            "method": "browser_navigate",
            "params": {"url": test_url},
            "id": "nav-1"
        }
        nav_response = await client.post(session_url, json=nav_payload, headers=headers)
        print(f"✅ Navigate response: {nav_response.status_code}")
        
        # Wait for page load
        await asyncio.sleep(3)
        
        # Snapshot
        print("📸 Taking snapshot...")
        snap_payload = {
            "jsonrpc": "2.0",
            "method": "browser_snapshot",
            "params": {},
            "id": "snap-1"
        }
        snap_response = await client.post(session_url, json=snap_payload, headers=headers)
        print(f"✅ Snapshot response: {snap_response.status_code}")
        
        # Parse result
        if snap_response.status_code == 200:
            lines = [line for line in snap_response.text.strip().split("\n") if line.startswith("data: ")]
            if lines:
                import json
                result_data = lines[-1].split("data: ")[1]
                result = json.loads(result_data)
                print(f"✅ Got result with {len(str(result))} characters")
                return True
            else:
                print("❌ No data lines found in response")
                return False
        else:
            print(f"❌ Snapshot failed with status {snap_response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Session test failed: {e}")
        return False

def main():
    """Main function"""
    print("🧪 Playwright Container Debug Test")
    print("=" * 50)
    
    try:
        success = asyncio.run(test_playwright_container())
        if success:
            print("\n🎉 All tests passed! Container is working correctly.")
            sys.exit(0)
        else:
            print("\n💥 Tests failed. Check the logs above for details.")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n⏹️  Test interrupted by user")
        sys.exit(1)

if __name__ == "__main__":
    main()