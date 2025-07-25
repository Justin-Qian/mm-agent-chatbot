import os
from typing import Annotated
import uuid
from backend.database import db_dependency, SessionLocal
from backend.document_parser import parse_pdf
from backend.models import File, User
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    status,
    BackgroundTasks,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from backend.routers.auth import get_current_user_from_cookie
from backend.chroma import add_file_to_chromadb, delete_file_from_chromadb


UPLOAD_DIR = "files"

os.makedirs(UPLOAD_DIR, exist_ok=True)


router = APIRouter(prefix="/files", tags=["files"])


class FileMetadataResponse(BaseModel):
    id: uuid.UUID
    name: str
    content_type: str
    size: int
    is_indexed: bool | None


async def index_file_in_background(file: File, file_path: str):
    """Background task to index a file in ChromaDB"""
    try:
        # Add file to ChromaDB
        await add_file_to_chromadb(
            file=file,
            file_path=file_path,
        )

        # Mark the file as indexed in the database
        db = SessionLocal()
        try:
            db_file = db.query(File).filter(File.id == file.id).first()
            if db_file:
                db_file.is_indexed = True
                db.commit()
        except Exception as e:
            db.rollback()
            print(f"Error updating file indexed status: {str(e)}")
        finally:
            db.close()

    except Exception as e:
        # Log the error but continue - background task shouldn't fail
        print(f"Error adding file to ChromaDB in background: {str(e)}")


@router.get("", response_model=list[FileMetadataResponse])
async def list_files(
    current_user: Annotated[User, Depends(get_current_user_from_cookie)],
    db: db_dependency,
):
    files = db.query(File).filter(File.user_id == current_user.id).all()
    return files


@router.post(
    "",
    response_model=FileMetadataResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user_from_cookie)],
    db: db_dependency,
):
    try:
        content = await file.read()

        new_file = File(
            user_id=current_user.id,
            name=file.filename,
            content_type=file.content_type,
            size=len(content),
            is_indexed=False,
        )

        db.add(new_file)
        db.commit()
        db.refresh(new_file)

        file_path = os.path.join(UPLOAD_DIR, str(new_file.id))
        with open(file_path, "wb") as f:
            f.write(content)

        # Schedule ChromaDB indexing as a background task
        background_tasks.add_task(
            index_file_in_background,
            file=new_file,
            file_path=file_path,
        )

        return new_file
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create file record",
        )
    except Exception as e:
        if "new_file" in locals() and new_file.id:
            db.delete(new_file)
            db.commit()

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload file: {str(e)}",
        )


@router.get("/{file_id}/download")
async def download_file(
    file_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user_from_cookie)],
    db: db_dependency,
):
    file = db.query(File).filter(File.id == file_id).first()

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    if file.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this file",
        )

    file_path = os.path.join(UPLOAD_DIR, str(file_id))
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File content not found",
        )

    return FileResponse(
        path=file_path, media_type=file.content_type, filename=file.name
    )


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_file(
    file_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user_from_cookie)],
    db: db_dependency,
):
    file = db.query(File).filter(File.id == file_id).first()

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    if file.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this file",
        )

    try:
        db.delete(file)
        db.commit()

        file_path = os.path.join(UPLOAD_DIR, str(file_id))
        if os.path.exists(file_path):
            os.remove(file_path)

        await delete_file_from_chromadb(file_id)

        return None

    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete file",
        )


@router.post("/{file_id}/parse")
async def parse_file(
    file_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user_from_cookie)],
    db: db_dependency,
):
    file = db.query(File).filter(File.id == file_id).first()

    if not file:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    if file.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this file",
        )

    file_path = os.path.join(UPLOAD_DIR, str(file_id))
    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File content not found",
        )

    with open(file_path, "rb") as f:
        pdf_bytes = f.read()

    parsed_document = await parse_pdf(pdf_bytes)

    return parsed_document
