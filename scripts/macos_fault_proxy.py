"""验收专用 CONNECT 转发器；不解密 TLS，不记录请求头或内容。"""
import select,socket,socketserver,threading,time

class FaultProxy(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True
    def __init__(self):
        self.connections=[];self.lock=threading.RLock();self.connects=0;self.reject=False;self.cuts=0
        super().__init__(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.serve_forever,daemon=True);self.thread.start()
    @property
    def url(self):return 'http://127.0.0.1:'+str(self.server_address[1])
    def drop(self):
        with self.lock:
            self.cuts+=1;self.reject=True
            for sock in list(self.connections):
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass
                try:sock.close()
                except OSError:pass
    def restore(self):self.reject=False
    def close(self):
        self.drop();self.shutdown();self.server_close();self.thread.join(2)

class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        local=self.request;remote=None
        try:
            local.settimeout(8);header=b''
            while b'\r\n\r\n' not in header and len(header)<65536:
                data=local.recv(4096)
                if not data:return
                header+=data
            line=header.split(b'\r\n',1)[0].decode('ascii')
            method,address,_=line.split(' ',2)
            if method!='CONNECT':return
            with self.server.lock:
                self.server.connects+=1
                if self.server.reject:
                    local.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n');return
            host,port=address.rsplit(':',1)
            remote=socket.create_connection((host,int(port)),timeout=8)
            local.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            remainder=header.split(b'\r\n\r\n',1)[1]
            if remainder:remote.sendall(remainder)
            local.settimeout(2);remote.settimeout(2)
            with self.server.lock:self.server.connections.extend((local,remote))
            while not self.server.reject:
                ready,_,_=select.select([local,remote],[],[],.2)
                for current in ready:
                    data=current.recv(65536)
                    if not data:return
                    (remote if current is local else local).sendall(data)
        except (OSError,ValueError):pass
        finally:
            with self.server.lock:
                for sock in (local,remote):
                    if sock in self.server.connections:self.server.connections.remove(sock)
            if remote:
                try:remote.close()
                except OSError:pass
