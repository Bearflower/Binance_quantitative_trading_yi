import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5)
sock.connect(('localhost', 8000))

request = b"POST /api/v1/send HTTP/1.1\r\nHost: localhost:8000\r\nContent-Type: application/json\r\nContent-Length: 54\r\nConnection: close\r\n\r\n{\"project\":\"btc_eth\",\"message\":\"test\",\"type\":\"text\"}\r\n"

print("发送请求...")
sock.sendall(request)
print("请求已发送，等待响应...")

response = b""
while True:
    try:
        chunk = sock.recv(1024)
        if not chunk:
            break
        response += chunk
        print(f"收到响应：{len(chunk)} bytes")
    except socket.timeout:
        print("超时")
        break

sock.close()
print(f"总响应：{len(response)} bytes")
if response:
    print(response.decode('utf-8', errors='ignore'))
