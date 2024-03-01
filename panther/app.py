import functools
import logging
from datetime import timedelta
from typing import Literal

from orjson import JSONDecodeError
from pydantic import ValidationError

from panther._utils import is_function_async
from panther.caching import (
    get_response_from_cache,
    set_response_in_cache,
    get_throttling_from_cache,
    increment_throttling_in_cache
)
from panther.configs import config
from panther.exceptions import (
    APIError,
    AuthorizationAPIError,
    JSONDecodeAPIError,
    MethodNotAllowedAPIError,
    ThrottlingAPIError,
    BadRequestAPIError
)
from panther.request import Request
from panther.response import Response
from panther.throttling import Throttling

__all__ = ('API', 'GenericAPI')

logger = logging.getLogger('panther')


class API:
    def __init__(
            self,
            *,
            input_model=None,
            output_model=None,
            auth: bool = False,
            permissions: list | None = None,
            throttling: Throttling = None,
            cache: bool = False,
            cache_exp_time: timedelta | int | None = None,
            methods: list[Literal['GET', 'POST', 'PUT', 'PATCH', 'DELETE']] | None = None,
    ):
        self.input_model = input_model
        self.output_model = output_model
        self.auth = auth
        self.permissions = permissions or []
        self.throttling = throttling
        self.cache = cache
        self.cache_exp_time = cache_exp_time
        self.methods = methods
        self.request: Request | None = None

    def __call__(self, func):
        @functools.wraps(func)
        async def wrapper(request: Request) -> Response:
            self.request: Request = request  # noqa: Non-self attribute could not be type hinted

            # 1. Check Method
            if self.methods and self.request.method not in self.methods:
                raise MethodNotAllowedAPIError

            # 2. Authentication
            await self.handle_authentication()

            # 3. Permissions
            await self.handle_permission()

            # 4. Throttling
            await self.handle_throttling()

            # 5. Validate Input
            if self.request.method in ['POST', 'PUT', 'PATCH']:
                self.handle_input_validation()

            # 6. Get Cached Response
            if self.cache and self.request.method == 'GET':
                if cached := await get_response_from_cache(request=self.request, cache_exp_time=self.cache_exp_time):
                    return Response(data=cached.data, status_code=cached.status_code)

            # 7. Put PathVariables and Request(If User Wants It) In kwargs
            kwargs = self.request.clean_parameters(func)

            # 8. Call Endpoint
            if is_function_async(func):
                response = await func(**kwargs)
            else:
                response = func(**kwargs)

            # 9. Clean Response
            if not isinstance(response, Response):
                response = Response(data=response)
            if self.output_model and response.data:
                response.data = response.apply_output_model(response.data, output_model=self.output_model)

            # 10. Set New Response To Cache
            if self.cache and self.request.method == 'GET':
                await set_response_in_cache(
                    request=self.request,
                    response=response,
                    cache_exp_time=self.cache_exp_time
                )

            # 11. Warning CacheExpTime
            if self.cache_exp_time and self.cache is False:
                logger.warning('"cache_exp_time" won\'t work while "cache" is False')

            return response

        return wrapper

    async def handle_authentication(self) -> None:
        if self.auth:
            if not config.AUTHENTICATION:
                logger.critical('"AUTHENTICATION" has not been set in configs')
                raise APIError
            user = await config.AUTHENTICATION.authentication(self.request)
            self.request.user = user

    async def handle_throttling(self) -> None:
        if throttling := self.throttling or config.THROTTLING:
            if await get_throttling_from_cache(self.request, duration=throttling.duration) + 1 > throttling.rate:
                raise ThrottlingAPIError

            await increment_throttling_in_cache(self.request, duration=throttling.duration)

    async def handle_permission(self) -> None:
        for perm in self.permissions:
            if type(perm.authorization).__name__ != 'method':
                logger.error(f'{perm.__name__}.authorization should be "classmethod"')
                raise AuthorizationAPIError
            if await perm.authorization(self.request) is False:
                raise AuthorizationAPIError

    def handle_input_validation(self):
        if self.input_model:
            self.request.validated_data = self.validate_input(model=self.input_model, request=self.request)

    @classmethod
    def validate_input(cls, model, request: Request):
        try:
            if isinstance(request.data, bytes):
                raise BadRequestAPIError(detail='Content-Type is not valid')
            return model(**request.data)
        except ValidationError as validation_error:
            error = {'.'.join(loc for loc in e['loc']): e['msg'] for e in validation_error.errors()}
            raise BadRequestAPIError(detail=error)
        except JSONDecodeError:
            raise JSONDecodeAPIError


class GenericAPI:
    input_model = None
    output_model = None
    auth: bool = False
    permissions: list | None = None
    throttling: Throttling | None = None
    cache: bool = False
    cache_exp_time: timedelta | int | None = None

    async def get(self, *args, **kwargs):
        raise MethodNotAllowedAPIError

    async def post(self, *args, **kwargs):
        raise MethodNotAllowedAPIError

    async def put(self, *args, **kwargs):
        raise MethodNotAllowedAPIError

    async def patch(self, *args, **kwargs):
        raise MethodNotAllowedAPIError

    async def delete(self, *args, **kwargs):
        raise MethodNotAllowedAPIError

    @classmethod
    async def call_method(cls, *args, **kwargs):
        match kwargs['request'].method:
            case 'GET':
                func = cls().get
            case 'POST':
                func = cls().post
            case 'PUT':
                func = cls().put
            case 'PATCH':
                func = cls().patch
            case 'DELETE':
                func = cls().delete

        return await API(
            input_model=cls.input_model,
            output_model=cls.output_model,
            auth=cls.auth,
            permissions=cls.permissions,
            throttling=cls.throttling,
            cache=cls.cache,
            cache_exp_time=cls.cache_exp_time,
        )(func)(*args, **kwargs)
