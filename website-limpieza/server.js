require('dotenv').config();
const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const cors = require('cors');
const path = require('path');

const redisClient = require('./utils/redisClient');
const requestTracker = require('./middleware/requestTracker');
const initializeWebSocket = require('./websocket/socketServer');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
    cors: {
        origin: '*',
        methods: ['GET', 'POST']
    }
});

const PORT = process.env.PORT || 3000;

// Middleware
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// Serve static files (dashboard)
app.use(express.static(path.join(__dirname, 'public')));

// Request tracking middleware (applies to all routes)
app.use(requestTracker);

// Routes
app.get('/health', (req, res) => {
    res.json({
        status: 'ok',
        redis: redisClient.isReady(),
        timestamp: Date.now()
    });
});

app.get('/api/test', (req, res) => {
    res.json({ message: 'Test endpoint working', timestamp: Date.now() });
});

// Debug endpoint to see exact counts
app.get('/debug', async (req, res) => {
    const total = await redisClient.get('metrics:requests:total');
    const allowed = await redisClient.get('metrics:requests:allowed');
    const blocked = await redisClient.get('metrics:requests:blocked');

    res.json({
        total: parseInt(total || '0'),
        allowed: parseInt(allowed || '0'),
        blocked: parseInt(blocked || '0'),
        redis: redisClient.isReady()
    });
});

// Reset counters
app.post('/debug/reset', async (req, res) => {
    await redisClient.set('metrics:requests:total', '0');
    await redisClient.set('metrics:requests:allowed', '0');
    await redisClient.set('metrics:requests:blocked', '0');

    res.json({ message: 'Counters reset', timestamp: Date.now() });
});

// Catch-all for dashboard
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

// Initialize services
async function startServer() {
    try {
        // Connect to Redis
        console.log('🔄 Connecting to Redis...');
        redisClient.connect();

        // Wait a bit for Redis to connect
        await new Promise(resolve => setTimeout(resolve, 1000));

        // Initialize WebSocket
        console.log('🔄 Initializing WebSocket server...');
        initializeWebSocket(io);

        // Start HTTP server
        server.listen(PORT, () => {
            console.log('');
            console.log('╔════════════════════════════════════════════╗');
            console.log('║   Layer 7 Monitoring Dashboard Started    ║');
            console.log('╚════════════════════════════════════════════╝');
            console.log('');
            console.log(`🌐 Dashboard: http://localhost:${PORT}`);
            console.log(`💚 Health:    http://localhost:${PORT}/health`);
            console.log(`🔌 WebSocket: ws://localhost:${PORT}`);
            console.log('');
            console.log('📊 Real-time metrics broadcasting active');
            console.log('');
        });
    } catch (error) {
        console.error('❌ Failed to start server:', error);
        process.exit(1);
    }
}

// Graceful shutdown
process.on('SIGTERM', () => {
    console.log('⚠️  SIGTERM received, shutting down gracefully...');
    server.close(() => {
        console.log('✅ Server closed');
        process.exit(0);
    });
});

process.on('SIGINT', () => {
    console.log('\n⚠️  SIGINT received, shutting down gracefully...');
    server.close(() => {
        console.log('✅ Server closed');
        process.exit(0);
    });
});

// Start the server
startServer();
