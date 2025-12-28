const Redis = require('ioredis');

class RedisClient {
    constructor() {
        this.client = null;
        this.isConnected = false;
    }

    connect() {
        const redisConfig = {
            host: process.env.REDIS_HOST || 'localhost',
            port: parseInt(process.env.REDIS_PORT) || 6379,
            retryStrategy: (times) => {
                const delay = Math.min(times * 50, 2000);
                return delay;
            },
            maxRetriesPerRequest: 3,
        };

        this.client = new Redis(redisConfig);

        this.client.on('connect', () => {
            console.log('✅ Connected to Redis');
            this.isConnected = true;
        });

        this.client.on('error', (err) => {
            console.error('❌ Redis error:', err.message);
            this.isConnected = false;
        });

        this.client.on('close', () => {
            console.log('⚠️  Redis connection closed');
            this.isConnected = false;
        });

        return this.client;
    }

    async get(key) {
        try {
            return await this.client.get(key);
        } catch (error) {
            console.error(`Error getting key ${key}:`, error.message);
            return null;
        }
    }

    async set(key, value, expirationSeconds = null) {
        try {
            if (expirationSeconds) {
                return await this.client.set(key, value, 'EX', expirationSeconds);
            }
            return await this.client.set(key, value);
        } catch (error) {
            console.error(`Error setting key ${key}:`, error.message);
            return null;
        }
    }

    async incr(key) {
        try {
            return await this.client.incr(key);
        } catch (error) {
            console.error(`Error incrementing key ${key}:`, error.message);
            return null;
        }
    }

    async zadd(key, score, member) {
        try {
            return await this.client.zadd(key, score, member);
        } catch (error) {
            console.error(`Error adding to sorted set ${key}:`, error.message);
            return null;
        }
    }

    async zrevrange(key, start, stop, withScores = false) {
        try {
            if (withScores) {
                return await this.client.zrevrange(key, start, stop, 'WITHSCORES');
            }
            return await this.client.zrevrange(key, start, stop);
        } catch (error) {
            console.error(`Error getting sorted set ${key}:`, error.message);
            return [];
        }
    }

    async lpush(key, ...values) {
        try {
            return await this.client.lpush(key, ...values);
        } catch (error) {
            console.error(`Error pushing to list ${key}:`, error.message);
            return null;
        }
    }

    async ltrim(key, start, stop) {
        try {
            return await this.client.ltrim(key, start, stop);
        } catch (error) {
            console.error(`Error trimming list ${key}:`, error.message);
            return null;
        }
    }

    async lrange(key, start, stop) {
        try {
            return await this.client.lrange(key, start, stop);
        } catch (error) {
            console.error(`Error getting list ${key}:`, error.message);
            return [];
        }
    }

    async expire(key, seconds) {
        try {
            return await this.client.expire(key, seconds);
        } catch (error) {
            console.error(`Error setting expiration for ${key}:`, error.message);
            return null;
        }
    }

    getClient() {
        return this.client;
    }

    isReady() {
        return this.isConnected;
    }
}

module.exports = new RedisClient();
