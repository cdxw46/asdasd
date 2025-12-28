/**
 * Traffic Simulator for testing the Layer 7 monitoring dashboard
 * Generates realistic HTTP traffic patterns
 */

const TARGET_URL = process.env.TARGET_URL || 'http://localhost:3000';

const endpoints = [
    '/api/test',
    '/api/users',
    '/api/products',
    '/api/orders',
    '/api/search',
    '/health',
    '/',
];

const userAgents = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    'curl/7.68.0',
    'PostmanRuntime/7.29.0',
];

class TrafficSimulator {
    constructor() {
        this.isRunning = false;
        this.totalRequests = 0;
    }

    // Make a single HTTP request
    async makeRequest(endpoint) {
        try {
            const userAgent = userAgents[Math.floor(Math.random() * userAgents.length)];

            const response = await fetch(`${TARGET_URL}${endpoint}`, {
                method: 'GET',
                headers: {
                    'User-Agent': userAgent,
                    'Accept': 'application/json',
                }
            });

            this.totalRequests++;

            if (this.totalRequests % 100 === 0) {
                console.log(`📊 Sent ${this.totalRequests} requests`);
            }

            return response.status;
        } catch (error) {
            console.error('Request failed:', error.message);
            return null;
        }
    }

    // Simulate steady traffic (constant RPS)
    async steadyTraffic(rps = 10, durationSeconds = 30) {
        console.log(`🚀 Starting steady traffic: ${rps} RPS for ${durationSeconds} seconds`);

        const interval = 1000 / rps;
        const endTime = Date.now() + (durationSeconds * 1000);

        while (Date.now() < endTime && this.isRunning) {
            const endpoint = endpoints[Math.floor(Math.random() * endpoints.length)];
            this.makeRequest(endpoint);
            await this.sleep(interval);
        }

        console.log('✅ Steady traffic completed');
    }

    // Simulate burst traffic (sudden spike)
    async burstTraffic(requestCount = 1000) {
        console.log(`💥 Starting burst traffic: ${requestCount} requests`);

        const batchSize = 20; // Send 20 at a time
        let completed = 0;

        for (let i = 0; i < requestCount; i += batchSize) {
            const batch = [];
            const limit = Math.min(i + batchSize, requestCount);

            for (let j = i; j < limit; j++) {
                const endpoint = endpoints[Math.floor(Math.random() * endpoints.length)];
                batch.push(this.makeRequest(endpoint));
            }

            await Promise.all(batch);
            completed += batch.length;

            // Tiny delay between batches to avoid overwhelming
            await this.sleep(5);
        }

        console.log('✅ Burst traffic completed');
        console.log(`📈 Total sent: ${this.totalRequests} requests`);
    }

    // Simulate wave pattern (oscillating RPS)
    async waveTraffic(minRPS = 5, maxRPS = 50, durationSeconds = 60) {
        console.log(`🌊 Starting wave traffic: ${minRPS}-${maxRPS} RPS for ${durationSeconds} seconds`);

        const startTime = Date.now();
        const endTime = startTime + (durationSeconds * 1000);
        const wavelength = 10000; // 10 seconds per wave

        while (Date.now() < endTime && this.isRunning) {
            const elapsed = Date.now() - startTime;
            const phase = (elapsed % wavelength) / wavelength;
            const sine = Math.sin(phase * 2 * Math.PI);
            const currentRPS = minRPS + ((sine + 1) / 2) * (maxRPS - minRPS);

            const endpoint = endpoints[Math.floor(Math.random() * endpoints.length)];
            this.makeRequest(endpoint);

            const interval = 1000 / currentRPS;
            await this.sleep(interval);
        }

        console.log('✅ Wave traffic completed');
    }

    // Simulate realistic mixed traffic
    async realisticTraffic(durationSeconds = 120) {
        console.log(`🎭 Starting realistic traffic for ${durationSeconds} seconds`);

        const endTime = Date.now() + (durationSeconds * 1000);
        const patterns = ['steady', 'burst', 'slow'];

        while (Date.now() < endTime && this.isRunning) {
            const pattern = patterns[Math.floor(Math.random() * patterns.length)];

            switch (pattern) {
                case 'steady':
                    await this.steadyTraffic(Math.floor(Math.random() * 20) + 5, 10);
                    break;
                case 'burst':
                    await this.burstTraffic(Math.floor(Math.random() * 200) + 50);
                    await this.sleep(2000);
                    break;
                case 'slow':
                    await this.steadyTraffic(2, 15);
                    break;
            }
        }

        console.log('✅ Realistic traffic completed');
    }

    // Helper: sleep function
    sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    // Start simulator
    start(mode = 'wave') {
        this.isRunning = true;
        console.log('');
        console.log('╔════════════════════════════════════════════╗');
        console.log('║      Traffic Simulator Started            ║');
        console.log('╚════════════════════════════════════════════╝');
        console.log('');
        console.log(`🎯 Target: ${TARGET_URL}`);
        console.log(`📝 Mode: ${mode}`);
        console.log('');

        switch (mode) {
            case 'steady':
                this.steadyTraffic(10, 300); // 10 RPS for 5 minutes
                break;
            case 'burst':
                this.burstTraffic(1000); // 1000 requests at once
                break;
            case 'wave':
                this.waveTraffic(5, 50, 300); // Wave pattern for 5 minutes
                break;
            case 'realistic':
                this.realisticTraffic(300); // Mixed traffic for 5 minutes
                break;
            default:
                console.error('Unknown mode. Use: steady, burst, wave, or realistic');
                this.isRunning = false;
        }
    }

    // Stop simulator
    stop() {
        console.log('\n⚠️  Stopping simulator...');
        this.isRunning = false;
    }
}

// Run simulator
const mode = process.argv[2] || 'wave';
const simulator = new TrafficSimulator();

// Handle graceful shutdown
process.on('SIGINT', () => {
    simulator.stop();
    setTimeout(() => {
        console.log(`\n✅ Total requests sent: ${simulator.totalRequests}`);
        process.exit(0);
    }, 1000);
});

process.on('SIGTERM', () => {
    simulator.stop();
    setTimeout(() => process.exit(0), 1000);
});

simulator.start(mode);
