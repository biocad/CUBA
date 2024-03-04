FROM node:9.11.2-jessie as build

RUN mkdir /code
WORKDIR /code
ADD package.json .
RUN npm install
ADD . .
RUN npm run build

FROM nginx:1.25-alpine3.18 as production
COPY --from=build /code/dist /var/www/cuba/
COPY nginx.conf /etc/nginx/nginx.conf