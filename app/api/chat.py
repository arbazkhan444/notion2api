"""OpenAI-compatible routes; orchestration lives in proxy_service."""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from starlette.concurrency import run_in_threadpool

from app.agent_adapter import is_agent_request
from app.config import is_lite_mode, is_standard_mode
from app.schemas import ChatCompletionRequest
from app.proxy_service import complete

router = APIRouter()


@router.post('/chat/completions', tags=['chat'])
async def create_chat_completion(request: Request, req_body: ChatCompletionRequest,
                                 response: Response, background_tasks: BackgroundTasks):
    mode = 'agent' if is_agent_request(req_body) else ('lite' if is_lite_mode() else 'standard' if is_standard_mode() else 'heavy')
    return await complete(request, req_body, response, background_tasks, mode=mode)


@router.delete('/conversations/{conversation_id}', tags=['chat'])
async def delete_conversation(conversation_id: str, request: Request):
    from app.conversation import ConversationManager
    # Local history remains deletable after changing APP_MODE.
    manager = getattr(request.app.state, 'conversation_manager', None)
    if manager is None:
        manager = await run_in_threadpool(ConversationManager)
    deleted = await run_in_threadpool(manager.delete_conversation, conversation_id)
    if not deleted:
        raise HTTPException(404, 'Conversation not found.')
    return {'id': conversation_id, 'deleted': True, 'scope': 'local_history'}
