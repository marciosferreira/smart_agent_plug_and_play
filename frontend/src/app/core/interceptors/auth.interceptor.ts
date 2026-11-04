import { inject } from '@angular/core';
import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { catchError, throwError } from 'rxjs';
import { UserSessionService } from '../services/user-session.service';

/**
 * Envia o JWT em Authorization: Bearer (POC do controle de acesso — ver auth.py no backend).
 * Sem token (backend sem controle de acesso) a requisição segue como antes.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const session = inject(UserSessionService);
  const token = session.token;
  const authed = token ? req.clone({ setHeaders: { Authorization: `Bearer ${token}` } }) : req;
  return next(authed).pipe(
    catchError((err: unknown) => {
      if (token && err instanceof HttpErrorResponse && err.status === 401) session.handleUnauthorized();
      return throwError(() => err);
    }),
  );
};
