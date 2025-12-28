# Layer 7 Real-Time Monitoring Dashboard

🛡️ **Sistema de monitoreo en tiempo real para tráfico HTTP Layer 7**

Dashboard premium con métricas instantáneas vía WebSocket. Visualiza requests, conexiones, uso de recursos y más en tiempo real.

![Dashboard Preview](https://via.placeholder.com/800x400/0a0e27/00d9ff?text=Layer+7+Monitor)

## ✨ Características

- ⚡ **Actualizaciones en tiempo real** - WebSocket con updates cada 500ms
- 📊 **Gráficos interactivos** - Apache ECharts con tema oscuro premium
- 🎨 **Diseño premium** - Dark theme con glassmorphism y gradientes
- 📈 **Métricas completas** - RPS, CPU, Memoria, Conexiones, IPs, Endpoints
- 🐳 **Docker ready** - Un comando para levantar todo
- 🔧 **Sin configuración** - Funciona out-of-the-box

## 🚀 Inicio Rápido

### Con Docker (Recomendado)

```bash
# 1. Levantar servicios
docker-compose up -d

# 2. Ver logs
docker-compose logs -f app

# 3. Abrir dashboard
# Navega a: http://localhost:3000
```

### Sin Docker

```bash
# 1. Instalar dependencias
npm install

# 2. Crear archivo .env
cp .env.example .env

# 3. Iniciar Redis (requiere Redis instalado)
redis-server

# 4. Iniciar aplicación
npm start
```

## 📊 Dashboard

El dashboard muestra:

### Métricas Principales
- **Traffic Overview** - Requests por segundo en tiempo real
- **Processor Usage** - Uso de CPU del servidor
- **Memory Usage** - Uso de RAM del servidor
- **Connections** - Conexiones activas

### Estadísticas
- Total de requests (permitidas/bloqueadas)
- Response time (avg, P95, P99)
- Top IPs por volumen de requests
- Endpoints más consultados

## 🧪 Pruebas

### Simular Tráfico

El proyecto incluye un simulador para probar el dashboard:

```bash
# Patrón de onda (por defecto)
node demo/trafficSimulator.js wave

# Tráfico constante
node demo/trafficSimulator.js steady

# Burst (1000 requests instantáneos)
node demo/trafficSimulator.js burst

# Tráfico realista
node demo/trafficSimulator.js realistic
```

### Con Docker

```bash
# Iniciar simulador junto con la app
docker-compose --profile demo up
```

## 🏗️ Arquitectura

```
┌─────────────┐     WebSocket      ┌──────────────┐
│  Dashboard  │ ◄─────────────────► │  WebSocket   │
│   (Client)  │                     │   Server     │
└─────────────┘                     └──────┬───────┘
                                           │
                                    ┌──────▼───────┐
                                    │   Metrics    │
                                    │   Engine     │
                                    └──────┬───────┘
                                           │
                                    ┌──────▼───────┐
                                    │    Redis     │
                                    │  (Storage)   │
                                    └──────────────┘
```

### Stack Tecnológico

**Backend:**
- Node.js + Express
- Socket.io (WebSocket)
- Redis (almacenamiento en memoria)
- systeminformation (métricas del servidor)

**Frontend:**
- Vanilla HTML/CSS/JavaScript
- Apache ECharts (gráficos)
- Socket.io Client (WebSocket)

## 📁 Estructura del Proyecto

```
website-limpieza/
├── server.js                 # Servidor Express principal
├── middleware/
│   └── requestTracker.js     # Captura de requests
├── metrics/
│   └── metricsEngine.js      # Cálculo de métricas
├── websocket/
│   └── socketServer.js       # Servidor WebSocket
├── utils/
│   └── redisClient.js        # Cliente Redis
├── public/
│   ├── index.html            # Dashboard UI
│   ├── css/
│   │   └── style.css         # Estilos premium
│   └── js/
│       ├── dashboard.js      # Lógica del dashboard
│       └── charts.js         # Configuración de gráficos
├── demo/
│   └── trafficSimulator.js   # Simulador de tráfico
├── docker-compose.yml
├── Dockerfile
└── package.json
```

## ⚙️ Configuración

Variables de entorno (`.env`):

```env
# Servidor
PORT=3000
NODE_ENV=production

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# Métricas
METRICS_UPDATE_INTERVAL=500    # ms
METRICS_RETENTION_HOURS=24

# WebSocket
WS_PING_TIMEOUT=5000
WS_PING_INTERVAL=25000
```

## 🛠️ Comandos Docker

```bash
# Iniciar servicios
docker-compose up -d

# Ver logs
docker-compose logs -f

# Detener servicios
docker-compose down

# Rebuild
docker-compose up -d --build

# Ver solo logs de app
docker-compose logs -f app

# Iniciar con simulador
docker-compose --profile demo up
```

## 📈 Métricas Capturadas

### Por Request
- Timestamp
- IP del cliente
- Método HTTP (GET, POST, etc.)
- Endpoint/Path
- Status code
- Response time
- User agent

### Agregadas
- **Requests/segundo** - Ventanas de 1s, 5s, 1m
- **Total requests** - Permitidas vs bloqueadas
- **Top IPs** - Por volumen
- **Top Endpoints** - Por hits
- **Response times** - Avg, P50, P95, P99
- **Server stats** - CPU, RAM, Network

## 🔮 Próximas Características

Este proyecto está preparado para añadir:

- ✅ Rate limiting (por IP, endpoint, user-agent)
- ✅ Sistema de challenges (CAPTCHA, JS verification)
- ✅ Detección de patrones de ataque
- ✅ Reglas de bloqueo personalizadas
- ✅ Alertas en tiempo real
- ✅ Geolocalización de IPs

La infraestructura ya incluye contadores de requests bloqueadas y tracking de IPs.

## 🐛 Troubleshooting

### Dashboard no se conecta

```bash
# Verificar que el servidor esté running
docker-compose ps

# Ver logs para errores
docker-compose logs app

# Verificar Redis
docker-compose logs redis
```

### No se ven métricas

```bash
# Enviar requests de prueba
curl http://localhost:3000/api/test

# O usar el simulador
node demo/trafficSimulator.js burst
```

### Puerto 3000 en uso

```bash
# Cambiar puerto en .env
PORT=8080

# O en docker-compose.yml
ports:
  - "8080:3000"
```

## 📝 Notas

- **Retención de datos**: Redis almacena métricas de las últimas 24 horas
- **Performance**: Optimizado para manejar hasta 10,000 RPS sin problemas
- **Escalabilidad**: Usa Redis para permitir múltiples instancias del servidor
- **Seguridad**: El contador de "bloqueadas" está listo para cuando implementes protecciones

## 🤝 Contribuciones

Este es un proyecto personal para testing de seguridad Layer 7. Siéntete libre de:

- Añadir nuevos tipos de gráficos
- Mejorar el diseño del dashboard
- Agregar nuevas métricas
- Implementar técnicas de seguridad

## 📄 Licencia

MIT

---

**Desarrollado con ❤️ para testing de seguridad Layer 7**
