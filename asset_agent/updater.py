import os
import sys
import time
import subprocess
import psutil
import logging

# Setup logging for the updater
log_dir = os.path.join(os.environ.get('PROGRAMDATA', 'C:\\ProgramData'), 'AssetAgent')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'updater.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def main():
    if len(sys.argv) < 4:
        logger.error("Usage: updater.exe <agent_pid> <agent_path> <new_agent_path>")
        sys.exit(1)

    agent_pid = int(sys.argv[1])
    agent_path = sys.argv[2]
    new_agent_path = sys.argv[3]

    logger.info(f"Starting update process...")
    logger.info(f"Waiting for agent (PID: {agent_pid}) to exit...")

    # Wait for the agent process to exit
    try:
        process = psutil.Process(agent_pid)
        process.wait(timeout=30)
    except psutil.NoSuchProcess:
        logger.info("Agent process already exited.")
    except psutil.TimeoutExpired:
        logger.error("Timed out waiting for agent to exit. Force killing...")
        process.kill()
    except Exception as e:
        logger.error(f"Error waiting for process: {e}")

    # Small delay to ensure file handles are released
    time.sleep(2)

    logger.info(f"Replacing {agent_path} with {new_agent_path}...")
    
    try:
        # Backup old agent (optional, but good for safety)
        backup_path = agent_path + ".bak"
        if os.path.exists(backup_path):
            os.remove(backup_path)
        
        if os.path.exists(agent_path):
            os.rename(agent_path, backup_path)
            logger.info("Backup created.")

        # Move new agent to the original path
        os.rename(new_agent_path, agent_path)
        logger.info("New agent installed.")

        # Restart the agent
        logger.info(f"Restarting agent: {agent_path}")
        subprocess.Popen([agent_path], creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)
        logger.info("Agent restarted successfully.")

    except Exception as e:
        logger.error(f"Update failed: {e}")
        # Try to restore backup if it exists and agent_path is missing
        if not os.path.exists(agent_path) and os.path.exists(agent_path + ".bak"):
            logger.info("Restoring backup...")
            os.rename(agent_path + ".bak", agent_path)
            subprocess.Popen([agent_path], creationflags=subprocess.CREATE_NO_WINDOW, close_fds=True)

    logger.info("Updater finished.")
    sys.exit(0)

if __name__ == "__main__":
    main()
