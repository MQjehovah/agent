docker build -t  agent:latest .
docker container stop agent
docker container rm agent
docker container run -it -d --restart unless-stopped --name agent -p 8090:8080 -v $(pwd)/config:/app/config -v $(pwd)/workspace:/app/workspace -v $(pwd)/logs:/app/logs agent:latest