const metricsEngine = require('../metrics/metricsEngine');

/**
 * Initialize WebSocket server for real-time metrics broadcasting
 */
function initializeWebSocket(io) {
    let metricsInterval;

    io.on('connection', (socket) => {
        console.log(`✅ Client connected: ${socket.id}`);

        // Send initial metrics immediately
        metricsEngine.calculateMetrics().then(metrics => {
            socket.emit('metrics:update', metrics);
        });

        // Start broadcasting metrics if not already running
        if (!metricsInterval) {
            const updateInterval = parseInt(process.env.METRICS_UPDATE_INTERVAL) || 500;

            metricsInterval = setInterval(async () => {
                try {
                    const metrics = await metricsEngine.calculateMetrics();
                    io.emit('metrics:update', metrics);
                } catch (error) {
                    console.error('Error broadcasting metrics:', error.message);
                }
            }, updateInterval);

            console.log(`📊 Broadcasting metrics every ${updateInterval}ms`);
        }

        // Handle client disconnect
        socket.on('disconnect', () => {
            console.log(`❌ Client disconnected: ${socket.id}`);

            // Stop broadcasting if no clients connected
            if (io.engine.clientsCount === 0 && metricsInterval) {
                clearInterval(metricsInterval);
                metricsInterval = null;
                console.log('⏸️  Stopped broadcasting (no clients)');
            }
        });

        // Handle client requests for immediate update
        socket.on('metrics:request', async () => {
            try {
                const metrics = await metricsEngine.calculateMetrics();
                socket.emit('metrics:update', metrics);
            } catch (error) {
                console.error('Error sending requested metrics:', error.message);
            }
        });
    });

    return io;
}

module.exports = initializeWebSocket;
