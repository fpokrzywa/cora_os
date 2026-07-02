FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY 40-cora-ui2-config.sh /docker-entrypoint.d/40-cora-ui2-config.sh
RUN chmod +x /docker-entrypoint.d/40-cora-ui2-config.sh

COPY index.html styles.css adapter.js config.js bootstrap.js shell.js scene.js /usr/share/nginx/html/
COPY image /usr/share/nginx/html/image

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -q --spider http://localhost/ || exit 1
