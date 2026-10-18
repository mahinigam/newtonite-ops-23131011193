# syntax=docker/dockerfile:1
#
# The web image: build the SPA, then serve the static files with nginx.
#
# The API base URL is built as the empty string on purpose. The nginx config forwards
# `/api` to the `api` service, so the browser talks to one origin and no CORS permission is
# needed in the containerised setup. Building the host name in would have meant an image
# that only works on the machine that built it.

FROM node:24-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

# `build` runs the type check first, so a type error fails the image build rather than
# shipping a bundle nobody verified.
RUN npm run build


FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
