FROM node:20-alpine

WORKDIR /app

# Install dependencies first so Docker can cache this layer
COPY package.json .
RUN npm install --omit=dev

COPY worker.js .

# The official node image ships a non-root user called "node"
USER node

CMD ["node", "worker.js"]
