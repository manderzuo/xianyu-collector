FROM nginx:alpine
COPY deploy/nginx.preview.conf /etc/nginx/conf.d/default.conf
COPY frontend/dist /usr/share/nginx/html
