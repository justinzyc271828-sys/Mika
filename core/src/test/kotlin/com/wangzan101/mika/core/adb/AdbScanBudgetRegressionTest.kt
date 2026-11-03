package com.wangzan101.mika.core.adb

import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AdbScanBudgetRegressionTest {
    @Test fun verifiesAnEarlyOpenPortBeforeTheFullSweepUsesTheBudget() = runTest {
        var now = 0L
        val verified = mutableListOf<Int>()
        val scanner = object : LocalPortScanner {
            override suspend fun openPorts(host: String, ports: IntRange, skip: Set<Int>, budgetMs: Long): List<Int> {
                val scanned = minOf(ports.count().toLong(), budgetMs).toInt()
                now += scanned
                return listOf(44339).filter { it in ports.first until ports.first + scanned && it !in skip }
            }
            override fun speaksAdbTls(host: String, port: Int, timeoutMs: Int): Boolean {
                verified += port
                now += 50
                return port == 44339
            }
        }
        val result = LocalPortScan(scanner, { listOf("127.0.0.1") },
            range = { 40000..50000 }, budgetMs = 10_000, now = { now }).scan(emptySet())
        assertEquals(listOf(44339), verified)
        assertEquals(listOf(AdbEndpoint("127.0.0.1", 44339)), result.endpoints)
        assertTrue(now <= 10_000)
    }
}
