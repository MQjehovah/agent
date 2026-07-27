docker build -t  agent:latest .
docker container stop agent
docker container rm agent
docker container run -it -d --name agent -p 8090:8080 -p 8091:8081 -v $(pwd)/config:/app/config -v $(pwd)/workspace:/app/workspace -v $(pwd)/logs:/app/logs agent:latest