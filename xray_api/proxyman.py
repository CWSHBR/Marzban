import grpc

from .base import XRayBase
from .exceptions import RelatedError
from .proto.app.proxyman.command import command_pb2, command_pb2_grpc
from .proto.common.protocol import user_pb2
from .types.account import Account
from .types.message import Message, TypedMessage

try:
    from .proto.core import config_pb2 as core_config_pb2
except ModuleNotFoundError:
    from .proto import config_pb2 as core_config_pb2


def _read_varint(buf: bytes, i: int):
    shift = value = 0
    while True:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7


def _encode_get_inbound_users_request(tag: str) -> bytes:
    """GetInboundUserRequest{tag = 1}. The bundled stubs predate this RPC."""
    raw = tag.encode()
    out = bytearray(b"\x0a")
    size = len(raw)
    while size >= 0x80:
        out.append((size & 0x7F) | 0x80)
        size >>= 7
    out.append(size)
    return bytes(out + raw)


def _decode_get_inbound_users_response(buf: bytes):
    """GetInboundUserResponse{repeated User users = 1}."""
    users = []
    i = 0
    while i < len(buf):
        key, i = _read_varint(buf, i)
        wire_type = key & 7
        if wire_type == 2:
            size, i = _read_varint(buf, i)
            if key >> 3 == 1:
                users.append(user_pb2.User.FromString(buf[i:i + size]))
            i += size
        elif wire_type == 0:
            _, i = _read_varint(buf, i)
        else:
            raise ValueError(f"unexpected wire type {wire_type} in GetInboundUserResponse")
    return users


class Proxyman(XRayBase):
    def get_inbound_users(self, tag: str, timeout: int = None) -> list:
        """Users the running inbound has right now (Xray 25+ HandlerService.GetInboundUsers)."""
        call = self._channel.unary_unary(
            "/xray.app.proxyman.command.HandlerService/GetInboundUsers",
            request_serializer=lambda payload: payload,
            response_deserializer=lambda payload: payload,
        )
        try:
            response = call(_encode_get_inbound_users_request(tag), timeout=timeout)
        except grpc.RpcError as e:
            raise RelatedError(e)
        return _decode_get_inbound_users_response(response)

    def alter_inbound(self, tag: str, operation: TypedMessage, timeout: int = None) -> bool:
        stub = command_pb2_grpc.HandlerServiceStub(self._channel)
        try:
            stub.AlterInbound(command_pb2.AlterInboundRequest(tag=tag, operation=operation), timeout=timeout)
            return True

        except grpc.RpcError as e:
            raise RelatedError(e)

    def alter_outbound(self, tag: str, operation: TypedMessage, timeout: int = None) -> bool:
        stub = command_pb2_grpc.HandlerServiceStub(self._channel)
        try:
            stub.AlterInbound(command_pb2.AlterOutboundRequest(tag=tag, operation=operation), timeout=timeout)
            return True

        except grpc.RpcError as e:
            raise RelatedError(e)

    def add_inbound_user(self, tag: str, user: Account, timeout: int = None) -> bool:
        return self.alter_inbound(
            tag=tag,
            operation=Message(
                command_pb2.AddUserOperation(
                    user=user_pb2.User(
                        level=user.level,
                        email=user.email,
                        account=user.message
                    )
                )
            ), timeout=timeout)

    def remove_inbound_user(self, tag: str, email: str, timeout: int = None) -> bool:
        return self.alter_inbound(
            tag=tag,
            operation=Message(
                command_pb2.RemoveUserOperation(
                    email=email
                )
            ), timeout=timeout)

    def add_outbound_user(self, tag: str, user: Account, timeout: int = None) -> bool:
        return self.alter_outbound(
            tag=tag,
            operation=Message(
                command_pb2.AddUserOperation(
                    user=user_pb2.User(
                        level=user.level,
                        email=user.email,
                        account=user.message
                    )
                )
            ), timeout=timeout)

    def remove_outbound_user(self, tag: str, email: str, timeout: int = None) -> bool:
        return self.alter_outbound(
            tag=tag,
            operation=Message(
                command_pb2.RemoveUserOperation(
                    email=email
                )
            ), timeout=timeout)

    def add_inbound(self):
        raise NotImplementedError

    def remove_inbound(self):
        raise NotImplementedError

    def add_outbound(self):
        raise NotImplementedError

    def remove_outbound(self):
        raise NotImplementedError
