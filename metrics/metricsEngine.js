const redisClient = require('../utils/redisClient');
const si = require('systeminformation');

class MetricsEngine {
    constructor() {
        this.lastMetrics = null;
    }

    /**
     * Calculate current metrics from Redis data
     */
    async calculateMetrics() {
        if (!redisClient.isReady()) {
            return this.lastMetrics || this.getDefaultMetrics();
        }

        try {
            const now = Date.now();

            // Get request counts
            const totalRequests = parseInt(await redisClient.get('metrics:requests:total') || '0');
            const allowedRequests = parseInt(await redisClient.get('metrics:requests:allowed') || '0');
            const blockedRequests = parseInt(await redisClient.get('metrics:requests:blocked') || '0');

            // Calculate requests per second for different time windows
            const rps1s = await this.calculateRPS(1);
            const rps5s = await this.calculateRPS(5);
            const rps1m = await this.calculateRPS(60);

            // Get top IPs
            const topIPs = await this.getTopIPs(10);

            // Get endpoint distribution
            const endpoints = await this.getEndpointStats(10);

            // Get response time statistics
            const responseStats = await this.getResponseTimeStats();

            // Get server resource usage
            const serverStats = await this.getServerStats();

            // Get connection stats (simulated for now, can be replaced with actual TCP stats)
            const connections = {
                active: Math.floor(rps1s * 2), // Rough estimate
                total: totalRequests,
                idle: Math.max(0, serverStats.connections - Math.floor(rps1s * 2))
            };

            const metrics = {
                timestamp: now,
                requests: {
                    total: totalRequests,
                    allowed: allowedRequests,
                    blocked: blockedRequests,
                    rps: {
                        current: rps1s,
                        avg5s: rps5s,
                        avg1m: rps1m,
                    }
                },
                topIPs,
                endpoints,
                responseTime: responseStats,
                server: serverStats,
                connections
            };

            this.lastMetrics = metrics;
            return metrics;
        } catch (error) {
            console.error('Error calculating metrics:', error.message);
            return this.lastMetrics || this.getDefaultMetrics();
        }
    }

    /**
     * Calculate requests per second for a given time window
     */
    async calculateRPS(seconds) {
        try {
            const now = Date.now();
            const windowStart = now - (seconds * 1000);

            const client = redisClient.getClient();
            const count = await client.zcount(
                'metrics:requests:timeseries',
                windowStart,
                now
            );

            return Math.round((count / seconds) * 10) / 10; // Round to 1 decimal
        } catch (error) {
            console.error('Error calculating RPS:', error.message);
            return 0;
        }
    }

    /**
     * Get top IP addresses by request count
     */
    async getTopIPs(limit = 10) {
        try {
            const ips = await redisClient.zrevrange('metrics:ips', 0, limit - 1);
            const result = [];

            for (const ip of ips) {
                const count = parseInt(await redisClient.get(`metrics:ip:${ip}:count`) || '0');
                result.push({ ip, count });
            }

            return result.sort((a, b) => b.count - a.count);
        } catch (error) {
            console.error('Error getting top IPs:', error.message);
            return [];
        }
    }

    /**
     * Get endpoint statistics
     */
    async getEndpointStats(limit = 10) {
        try {
            const client = redisClient.getClient();
            const keys = await client.keys('metrics:endpoint:*');
            const endpoints = [];

            for (const key of keys) {
                const path = key.replace('metrics:endpoint:', '');
                const count = parseInt(await redisClient.get(key) || '0');
                endpoints.push({ path, count });
            }

            return endpoints
                .sort((a, b) => b.count - a.count)
                .slice(0, limit);
        } catch (error) {
            console.error('Error getting endpoint stats:', error.message);
            return [];
        }
    }

    /**
     * Get response time statistics
     */
    async getResponseTimeStats() {
        try {
            const times = await redisClient.lrange('metrics:response:times', 0, -1);

            if (times.length === 0) {
                return { avg: 0, p50: 0, p95: 0, p99: 0 };
            }

            const numericTimes = times.map(t => parseInt(t)).sort((a, b) => a - b);
            const avg = Math.round(numericTimes.reduce((a, b) => a + b, 0) / numericTimes.length);

            const p50 = numericTimes[Math.floor(numericTimes.length * 0.5)];
            const p95 = numericTimes[Math.floor(numericTimes.length * 0.95)];
            const p99 = numericTimes[Math.floor(numericTimes.length * 0.99)];

            return { avg, p50, p95, p99 };
        } catch (error) {
            console.error('Error calculating response time stats:', error.message);
            return { avg: 0, p50: 0, p95: 0, p99: 0 };
        }
    }

    /**
     * Get server resource statistics
     */
    async getServerStats() {
        try {
            const [cpuLoad, memory, networkStats] = await Promise.all([
                si.currentLoad(),
                si.mem(),
                si.networkStats()
            ]);

            return {
                cpu: {
                    usage: Math.round(cpuLoad.currentLoad * 10) / 10,
                    cores: cpuLoad.cpus.length
                },
                memory: {
                    total: Math.round(memory.total / (1024 * 1024 * 1024) * 100) / 100, // GB
                    used: Math.round(memory.used / (1024 * 1024 * 1024) * 100) / 100, // GB
                    percentage: Math.round((memory.used / memory.total) * 100 * 10) / 10
                },
                network: {
                    rx: networkStats[0]?.rx_sec || 0,
                    tx: networkStats[0]?.tx_sec || 0
                },
                connections: Math.floor(Math.random() * 50) + 10 // Placeholder
            };
        } catch (error) {
            console.error('Error getting server stats:', error.message);
            return {
                cpu: { usage: 0, cores: 1 },
                memory: { total: 0, used: 0, percentage: 0 },
                network: { rx: 0, tx: 0 },
                connections: 0
            };
        }
    }

    /**
     * Get default metrics structure
     */
    getDefaultMetrics() {
        return {
            timestamp: Date.now(),
            requests: {
                total: 0,
                allowed: 0,
                blocked: 0,
                rps: { current: 0, avg5s: 0, avg1m: 0 }
            },
            topIPs: [],
            endpoints: [],
            responseTime: { avg: 0, p50: 0, p95: 0, p99: 0 },
            server: {
                cpu: { usage: 0, cores: 1 },
                memory: { total: 0, used: 0, percentage: 0 },
                network: { rx: 0, tx: 0 },
                connections: 0
            },
            connections: { active: 0, total: 0, idle: 0 }
        };
    }
}

module.exports = new MetricsEngine();
