---
name: deploy
description: 你有公司业务系统rosiwit-cloud的部署能力
---
# rosiwit-cloud部署

**适用场景**: 用于将rosiwit-cloud业务系统部署到目标服务器上

## 操作规范

1. SSH登录目标机器
2. 部署基础依赖/中间件(mysql、redis、)
3. 部署业务系统

## 具体操作

### Nginx安装

```bash
mkdir -p ~/docker/nginx/html
mkdir -p ~/docker/nginx/conf.d
mkdir -p ~/docker/nginx/cert
mkdir -p ~/docker/nginx/logs
```

`docker run -d --net=host --name nginx -v ~/docker/nginx/html:/usr/share/nginx/html -v ~/docker/nginx/conf.d:/etc/nginx/conf.d -v ~/docker/nginx/cert:/etc/nginx/cert -v ~/docker/nginx/logs:/var/log/nginx --privileged=true nginx`

defaut.conf

```bash
# 管理后台HTTP配置
server {
    listen       80 default_server;
    listen       [::]:80 default_server;
    server_name  _;
    server_name  bms-cn.xzrobot.com bms-cn.rosiwit.com;

    location / {
        return 301 /admin;
    }
}

# 管理后台HTTPS配置
server {
    listen 443 ssl;
    server_name  bms-cn.rosiwit.com;

    ssl_certificate      /etc/nginx/cert/rosiwit.com.pem;
    ssl_certificate_key  /etc/nginx/cert/rosiwit.com.key;

}

server {
    listen 443 ssl;
    server_name  bms-cn.xzrobot.com;

    ssl_certificate      /etc/nginx/cert/xzrobot.com.pem;
    ssl_certificate_key  /etc/nginx/cert/xzrobot.com.key;

    ssl_protocols TLSv1.2 TLSv1.3; #表示使用的TLS协议的类型
    ssl_prefer_server_ciphers on;

    location / {
        return 301 /admin;
    }

    location /admin {
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_pass http://localhost:8080/;
    }

}
```

### Mysql安装

```
mkdir -p ~/docker/mysql/data
```

`docker run -d --name mysql -p 3306:3306 -v ~/docker/mysql/data:/var/lib/mysql -e MYSQL_ROOT_PASSWORD=xzyz2022! mysql`

## Redis安装

```
mkdir -p ~/docker/redis/data
```

`docker run --name redis -p 6379:6379 -v ~/docker/redis/data:/data -d redis redis-server --appendonly yes --requirepass "xzyz2022!"`
