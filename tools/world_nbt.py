"""Read and preserve Bedrock level.dat tags, changing only Beta API flags."""
import struct


def load(raw):
    pos = 8

    def read(fmt):
        nonlocal pos
        size = struct.calcsize('<' + fmt)
        result = struct.unpack_from('<' + fmt, raw, pos)[0]
        pos += size
        return result

    def string():
        nonlocal pos
        size = read('H')
        value = raw[pos:pos + size].decode('utf-8')
        pos += size
        return value

    def value(kind):
        nonlocal pos
        if kind in (1, 2, 3, 4, 5, 6):
            return read({1:'b', 2:'h', 3:'i', 4:'q', 5:'f', 6:'d'}[kind])
        if kind == 7:
            size = read('i')
            result = raw[pos:pos + size]
            pos += size
            return result
        if kind == 8:
            return string()
        if kind == 9:
            subtype, size = read('b'), read('i')
            return subtype, [value(subtype) for _ in range(size)]
        if kind == 10:
            result = {}
            while True:
                subtype = read('b')
                if not subtype:
                    return result
                name = string()
                result[name] = (subtype, value(subtype))
        if kind in (11, 12):
            return [read('i' if kind == 11 else 'q') for _ in range(read('i'))]
        raise ValueError('Unknown NBT type')

    kind, name = read('b'), string()
    result = kind, name, value(kind)
    if pos != len(raw):
        raise ValueError('Unexpected trailing level.dat bytes')
    return result


def dump(root, header_version=10):
    def string(value):
        encoded = value.encode('utf-8')
        return struct.pack('<H', len(encoded)) + encoded

    def value(kind, item):
        if kind in (1, 2, 3, 4, 5, 6):
            return struct.pack('<' + {1:'b', 2:'h', 3:'i', 4:'q', 5:'f', 6:'d'}[kind], item)
        if kind == 7:
            return struct.pack('<i', len(item)) + item
        if kind == 8:
            return string(item)
        if kind == 9:
            subtype, elements = item
            return struct.pack('<bi', subtype, len(elements)) + b''.join(value(subtype, x) for x in elements)
        if kind == 10:
            return b''.join(bytes([subtype]) + string(name) + value(subtype, data) for name, (subtype, data) in item.items()) + b'\0'
        if kind in (11, 12):
            return struct.pack('<i', len(item)) + b''.join(struct.pack('<i' if kind == 11 else '<q', x) for x in item)
        raise ValueError('Unknown NBT type')

    kind, name, data = root
    body = bytes([kind]) + string(name) + value(kind, data)
    return struct.pack('<II', header_version, len(body)) + body
