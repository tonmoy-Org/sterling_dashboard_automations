import paramiko
import sys

def execute_command(ssh, command):
    print(f"Executing: {command}")
    stdin, stdout, stderr = ssh.exec_command(command)
    exit_status = stdout.channel.recv_exit_status()
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(f"STDOUT:\n{out}")
    if err:
        print(f"STDERR:\n{err}")
    return exit_status, out, err

try:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect('38.54.110.239', username='root', password='Sdashboard1@')
    print("Successfully connected to the VPS.")
    
    # Let's find the project directory
    _, out, _ = execute_command(ssh, "find /root /var/www /opt /home -maxdepth 3 -name 'docker-compose.yml' -type f 2>/dev/null")
    
    paths = out.split('\n')
    target_dir = None
    for path in paths:
        if 'Sterling-Dashboard_Automations' in path:
            target_dir = path.replace('/docker-compose.yml', '')
            break
            
    if not target_dir and paths and paths[0]:
        target_dir = paths[0].replace('/docker-compose.yml', '')
        
    if target_dir:
        print(f"Found deployment directory: {target_dir}")
        # Execute pull and redeploy
        execute_command(ssh, f"cd {target_dir} && git pull")
        execute_command(ssh, f"cd {target_dir} && docker-compose down")
        execute_command(ssh, f"cd {target_dir} && docker-compose build")
        execute_command(ssh, f"cd {target_dir} && docker-compose up -d")
    else:
        print("Could not find deployment directory containing docker-compose.yml.")
        
    ssh.close()
except Exception as e:
    print(f"An error occurred: {e}")
