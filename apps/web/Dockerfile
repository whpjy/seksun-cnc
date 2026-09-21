FROM node:24-alpine AS builder
ARG VITE_APP_NAME=NEXUS CNC
ARG VITE_APP_LOGO_TEXT=N
ENV VITE_APP_NAME=$VITE_APP_NAME
ENV VITE_APP_LOGO_TEXT=$VITE_APP_LOGO_TEXT
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
