"""BMCU wire protocol: COBS framing, packet encoding and the stream decoder."""
import os
import struct
import unittest

import _support  # noqa: F401  (path setup)
from bmcu_core import protocol


class CobsTest(unittest.TestCase):
    def roundtrip(self, data):
        encoded = protocol.cobs_encode(data)
        self.assertNotIn(0, bytearray(encoded), 'COBS output must not contain zero bytes')
        self.assertEqual(protocol.cobs_decode(encoded), data)

    def test_empty_and_zero_runs(self):
        for data in (b'', b'\x00', b'\x00\x00\x00', b'\x01\x00\x02', b'\x11\x22\x00\x33'):
            self.roundtrip(data)

    def test_block_boundaries(self):
        # 254 and 255 non-zero bytes cross COBS's maximum block length
        for size in (253, 254, 255, 256, 508, 509, 600):
            self.roundtrip(bytes((i % 255) + 1 for i in range(size)))

    def test_random_payloads(self):
        for _ in range(200):
            self.roundtrip(os.urandom(int.from_bytes(os.urandom(1), 'little')))

    def test_rejects_zero_byte_and_truncation(self):
        with self.assertRaises(ValueError):
            protocol.cobs_decode(b'\x02\x01\x00\x01')   # zero where a code byte belongs
        with self.assertRaises(ValueError):
            protocol.cobs_decode(b'\x05\x01\x02')


class PacketTest(unittest.TestCase):
    def unframe(self, frame):
        self.assertEqual(frame[:1], b'\x00')
        self.assertEqual(frame[-1:], b'\x00')
        return protocol.cobs_decode(frame[1:-1])

    def test_roundtrip(self):
        frame = protocol.encode_packet(0x19, 7, 513, b'\x00payload\x00')
        self.assertEqual(protocol.decode_packet(self.unframe(frame)),
                         (0x19, 7, 513, b'\x00payload\x00'))

    def test_crc_mismatch(self):
        decoded = bytearray(self.unframe(protocol.encode_packet(1, 2, 3, b'abc')))
        decoded[-5] ^= 0xff
        with self.assertRaisesRegex(ValueError, 'CRC'):
            protocol.decode_packet(bytes(decoded))

    def test_short_packet(self):
        with self.assertRaises(ValueError):
            protocol.decode_packet(b'\x01\x02')

    def test_wrong_version(self):
        decoded = bytearray(self.unframe(protocol.encode_packet(1, 2, 3, b'abc')))
        body = bytearray(decoded[:-4])
        body[0] = (protocol.PROTO_VERSION + 1) & 0xff
        repacked = bytes(body) + struct.pack('<I', protocol.crc32(bytes(body)))
        with self.assertRaises(protocol.ProtocolVersionError):
            protocol.decode_packet(repacked)


class StreamDecoderTest(unittest.TestCase):
    def test_split_feeds_and_noise(self):
        decoder = protocol.StreamDecoder()
        stream = (b'\x13\x37garbage' + protocol.encode_packet(1, 1, 10, b'one') +
                  protocol.encode_packet(2, 2, 20, b'two'))
        packets = []
        for i in range(0, len(stream), 3):
            packets.extend(decoder.feed(stream[i:i + 3]))
        self.assertEqual([(p[0], p[3]) for p in packets], [(1, b'one'), (2, b'two')])
        self.assertGreaterEqual(decoder.errors, 1)  # the garbage before the first frame

    def test_oversize_frame_is_discarded_then_recovers(self):
        decoder = protocol.StreamDecoder(max_frame=32)
        oversize = b'\x00' + b'\x01' * 100 + b'\x00'
        packets = decoder.feed(oversize + protocol.encode_packet(3, 3, 30, b'ok'))
        self.assertEqual([(p[0], p[3]) for p in packets], [(3, b'ok')])
        self.assertGreaterEqual(decoder.errors, 1)

    def test_version_mismatch_is_reported(self):
        decoder = protocol.StreamDecoder()
        frame = protocol.encode_packet(1, 1, 1, b'x')
        decoded = bytearray(protocol.cobs_decode(frame[1:-1]))
        body = bytearray(decoded[:-4])
        body[0] = (protocol.PROTO_VERSION + 1) & 0xff
        bad = b'\x00' + protocol.cobs_encode(
            bytes(body) + struct.pack('<I', protocol.crc32(bytes(body)))) + b'\x00'
        self.assertEqual(decoder.feed(bad), [])
        self.assertEqual(decoder.incompatible_version, (protocol.PROTO_VERSION + 1) & 0xff)



class RuntimeSyncAutoloadTest(unittest.TestCase):
    """Channel autoload goes up to 2 m with BMCU firmware 1.0.5."""
    VALUES = {protocol.CONFIG_LOAD_PRESSURE_PCT: 0, protocol.CONFIG_LOAD_SPEED_MMS: 60.0,
              protocol.CONFIG_PULL_SPEED_MMS: 60.0, protocol.CONFIG_PULL_SPEED_END_MMS: 10.0,
              protocol.CONFIG_JAM_TIMEOUT_MS: 20000.0,
              protocol.CONFIG_BEFORE_PULLBACK_TARGET_PCT: 40.0}

    def pack(self, autoload):
        return protocol.pack_runtime_sync(self.VALUES, (0, 0, 0),
                                          channel_autoload_m=autoload)

    def test_two_metres_accepted_and_encoded(self):
        payload = self.pack((2.0, 1.5, 0.12, 0.01))
        self.assertEqual(len(payload), 63)
        autoload = struct.unpack_from('<4f', payload, 40)
        self.assertAlmostEqual(autoload[0], 2.0)
        self.assertAlmostEqual(autoload[1], 1.5)

    def test_over_two_metres_rejected(self):
        with self.assertRaises(ValueError):
            self.pack((2.001, 0.12, 0.12, 0.12))
        with self.assertRaises(ValueError):
            self.pack((0.005, 0.12, 0.12, 0.12))


if __name__ == '__main__':
    unittest.main()
