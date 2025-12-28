const redisClient = require('../utils/redisClient');

/**
 * Middleware to track all incoming HTTP requests
 */
const requestTracker = async (req, res, next) => {
    const startTime = Date.now();

    // Extract request metadata
    const requestData = {
        timestamp: Date.now(),
        ip: req.ip || req.connection.remoteAddress || 'unknown',
        method: req.method,
        path: req.path,
        userAgent: req.get('user-agent') || 'unknown',
    };

    // Track request in Redis
    if (redisClient.isReady()) {
        try {
            // Increment total requests counter
            await redisClient.incr('metrics:requests:total');

            // Add request to time-series (for requests/sec calculation)
            await redisClient.zadd(
                'metrics:requests:timeseries',
                requestData.timestamp,
                `${requestData.timestamp}:${requestData.ip}:${requestData.path}`
            );

            // Track IP addresses
            await redisClient.zadd('metrics:ips', Date.now(), requestData.ip);
            await redisClient.incr(`metrics:ip:${requestData.ip}:count`);

            // Track endpoint hits
            await redisClient.incr(`metrics:endpoint:${requestData.path}`);

            // Keep time-series data for last 5 minutes only
            const fiveMinutesAgo = Date.now() - (5 * 60 * 1000);
            await redisClient.getClient().zremrangebyscore(
                'metrics:requests:timeseries',
                '-inf',
                fiveMinutesAgo
            );
        } catch (error) {
            console.error('Error tracking request in Redis:', error.message);
        }
    }

    // Capture response time and status code
    res.on('finish', async () => {
        const responseTime = Date.now() - startTime;
        const statusCode = res.statusCode;

        if (redisClient.isReady()) {
            try {
                // Track response times for percentile calculations
                await redisClient.lpush('metrics:response:times', responseTime);
                await redisClient.ltrim('metrics:response:times', 0, 999); // Keep last 1000

                // Track status codes
                await redisClient.incr(`metrics:status:${statusCode}`);

                // Track blocked requests (status >= 400)
                if (statusCode >= 400) {
                    await redisClient.incr('metrics:requests:blocked');
                } else {
                    await redisClient.incr('metrics:requests:allowed');
                }
            } catch (error) {
                console.error('Error tracking response metrics:', error.message);
            }
        }
    });

    next();
};

module.exports = requestTracker;
