cd /home/anthares/Documentos/Projetos/Gabreu/chat-fastapi/manete/Iniciar.sh

source .venv/bin/activate

uvicorn server:app --host 0.0.0.0 --port 8081 --reload