FROM node:24-alpine AS builder
ARG VITE_APP_NAME=NEXUS CNC
ARG VITE_PAGE_TITLE=智能工艺规划
ARG VITE_APP_LOGO_TEXT=N
ARG VITE_HARNESS_URL=http://127.0.0.1:3080
ENV VITE_APP_NAME=$VITE_APP_NAME
ENV VITE_PAGE_TITLE=$VITE_PAGE_TITLE
ENV VITE_APP_LOGO_TEXT=$VITE_APP_LOGO_TEXT
ENV VITE_HARNESS_URL=$VITE_HARNESS_URL
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html
EXPOSE 80
